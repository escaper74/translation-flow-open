from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from media_planning.contracts import MediaRepackageRequest
from media_planning.runner import MediaRepackageError, MediaRepackageRunner


@unittest.skipUnless(
    os.environ.get("MEDIA_REPACKAGE_TOOL_ROOT"),
    "set MEDIA_REPACKAGE_TOOL_ROOT for local FFmpeg media acceptance",
)
class MediaRepackageSyntheticTests(unittest.TestCase):
    @staticmethod
    def _tools() -> tuple[Path, Path]:
        tool_root = Path(os.environ["MEDIA_REPACKAGE_TOOL_ROOT"])
        candidates = [
            tool_root / "ffmpeg.exe",
            tool_root
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
            / "ffmpeg.exe",
        ]
        ffmpeg = next((path for path in candidates if path.is_file()), None)
        if ffmpeg is None:
            raise unittest.SkipTest("FFmpeg was not found under MEDIA_REPACKAGE_TOOL_ROOT")
        ffprobe = ffmpeg.with_name("ffprobe.exe")
        if not ffprobe.is_file():
            raise unittest.SkipTest("FFprobe was not found beside FFmpeg")
        return ffmpeg, ffprobe

    @staticmethod
    def _run(args: list[str]) -> None:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=90,
        )
        if result.returncode != 0:
            raise AssertionError((result.stderr or result.stdout)[-2400:])

    @staticmethod
    def _probe(ffprobe: Path, media: Path) -> dict:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(media),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return json.loads(result.stdout)

    def _make_source(self, root: Path, ffmpeg: Path) -> Path:
        english = root / "english.srt"
        chinese = root / "chinese.srt"
        english.write_text(
            "1\n00:00:00,100 --> 00:00:04,500\nEnglish\n",
            encoding="utf-8",
        )
        chinese.write_text(
            "1\n00:00:00,100 --> 00:00:04,500\n合成中文字幕\n",
            encoding="utf-8",
        )
        source = root / "container-facts-not-suffix.data"
        self._run(
            [
                str(ffmpeg),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=10:duration=5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=5",
                "-f",
                "srt",
                "-i",
                str(english),
                "-f",
                "srt",
                "-i",
                str(chinese),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:a:0",
                "-map",
                "3:0",
                "-map",
                "4:0",
                "-t",
                "5",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                "-disposition:a:0",
                "0",
                "-disposition:a:1",
                "0",
                "-metadata:s:s:0",
                "language=eng",
                "-disposition:s:0",
                "0",
                "-metadata:s:s:1",
                "language=zho",
                "-disposition:s:1",
                "default",
                "-f",
                "matroska",
                str(source),
            ]
        )
        return source

    def test_real_mkv_and_mp4_keep_video_all_audio_and_one_soft_subtitle(self) -> None:
        ffmpeg, ffprobe = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._make_source(root, ffmpeg)
            source_probe = self._probe(ffprobe, source)
            source_video_codecs = [
                item["codec_name"]
                for item in source_probe["streams"]
                if item["codec_type"] == "video"
            ]
            source_audio_codecs = [
                item["codec_name"]
                for item in source_probe["streams"]
                if item["codec_type"] == "audio"
            ]
            self.assertEqual(
                [0, 0],
                [
                    item["disposition"]["default"]
                    for item in source_probe["streams"]
                    if item["codec_type"] == "audio"
                ],
            )

            for target, subtitle_codec in (("mkv", "subrip"), ("mp4", "mov_text")):
                with self.subTest(target=target):
                    output_dir = root / f"out-{target}"
                    request = MediaRepackageRequest.from_mapping(
                        {
                            "schema_version": 1,
                            "workflow_mode": "media_repackage",
                            "job_id": f"synthetic-{target}",
                            "source_media": str(source),
                            "output_dir": str(output_dir),
                            "output_target": target,
                        }
                    )
                    result = MediaRepackageRunner(
                        request,
                        ffmpeg=ffmpeg,
                        ffprobe=ffprobe,
                    ).run()
                    output = Path(result["outputs"]["final_media"])
                    output_probe = self._probe(ffprobe, output)
                    videos = [
                        item
                        for item in output_probe["streams"]
                        if item["codec_type"] == "video"
                    ]
                    audios = [
                        item
                        for item in output_probe["streams"]
                        if item["codec_type"] == "audio"
                    ]
                    subtitles = [
                        item
                        for item in output_probe["streams"]
                        if item["codec_type"] == "subtitle"
                    ]
                    self.assertEqual(
                        source_video_codecs,
                        [item["codec_name"] for item in videos],
                    )
                    self.assertEqual(
                        source_audio_codecs,
                        [item["codec_name"] for item in audios],
                    )
                    if target == "mkv":
                        self.assertEqual(
                            [0, 0],
                            [item["disposition"]["default"] for item in audios],
                        )
                    self.assertEqual(1, len(subtitles))
                    self.assertEqual(subtitle_codec, subtitles[0]["codec_name"])
                    self.assertEqual("zho", subtitles[0]["tags"]["language"])
                    self.assertEqual(1, subtitles[0]["disposition"]["default"])
                    report = json.loads(
                        request.technical_report_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual("passed", report["technical_validation"]["status"])
                    self.assertEqual(
                        3,
                        len(
                            report["technical_validation"][
                                "short_decode_positions_seconds"
                            ]
                        ),
                    )
                    self.assertTrue(
                        report["technical_validation"]["video_audio_stream_copy"]
                    )

    def test_real_prores_pcm_mov_reaches_ffmpeg_and_direct_muxes_to_mkv(self) -> None:
        ffmpeg, ffprobe = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subtitle = root / "chinese.srt"
            subtitle.write_text(
                "1\n00:00:00,100 --> 00:00:01,500\n中文\n",
                encoding="utf-8",
            )
            source = root / "prores-pcm-mov.unknown"
            self._run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-f",
                    "srt",
                    "-i",
                    str(subtitle),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map",
                    "2:0",
                    "-c:v",
                    "prores_ks",
                    "-profile:v",
                    "0",
                    "-c:a",
                    "pcm_s16le",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=chi",
                    "-disposition:s:0",
                    "default",
                    "-f",
                    "mov",
                    str(source),
                ]
            )
            request = MediaRepackageRequest.from_mapping(
                {
                    "schema_version": 1,
                    "workflow_mode": "media_repackage",
                    "job_id": "synthetic-prores-pcm",
                    "source_media": str(source),
                    "output_dir": str(root / "out"),
                    "output_target": "mkv",
                }
            )
            result = MediaRepackageRunner(
                request,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            ).run()
            output_probe = self._probe(
                ffprobe, Path(result["outputs"]["final_media"])
            )
            self.assertEqual(
                ["prores"],
                [
                    item["codec_name"]
                    for item in output_probe["streams"]
                    if item["codec_type"] == "video"
                ],
            )
            self.assertEqual(
                ["pcm_s16le"],
                [
                    item["codec_name"]
                    for item in output_probe["streams"]
                    if item["codec_type"] == "audio"
                ],
            )

    def test_real_incompatible_mp4_plan_stops_before_ffmpeg_mux(self) -> None:
        ffmpeg, ffprobe = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subtitle = root / "chinese.srt"
            subtitle.write_text(
                "1\n00:00:00,100 --> 00:00:01,500\n中文\n",
                encoding="utf-8",
            )
            source = root / "ffv1-pcm.mkv"
            self._run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-f",
                    "srt",
                    "-i",
                    str(subtitle),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map",
                    "2:0",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s24le",
                    "-c:s",
                    "srt",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-disposition:s:0",
                    "default",
                    str(source),
                ]
            )
            request = MediaRepackageRequest.from_mapping(
                {
                    "schema_version": 1,
                    "workflow_mode": "media_repackage",
                    "job_id": "synthetic-incompatible",
                    "source_media": str(source),
                    "output_dir": str(root / "out"),
                    "output_target": "mp4",
                }
            )
            calls: list[list[str]] = []

            def recording_runner(args: list[str], **kwargs: object):
                calls.append(list(args))
                return subprocess.run(args, **kwargs)

            with self.assertRaisesRegex(
                MediaRepackageError, "需要转码或改用 MKV"
            ):
                MediaRepackageRunner(
                    request,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    process_runner=recording_runner,
                ).run()
            self.assertEqual([str(ffprobe)], [args[0] for args in calls])
            self.assertFalse(request.output_path.exists())

    def test_keyless_cli_emits_the_formal_jsonl_completion_contract(self) -> None:
        ffmpeg, _ = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._make_source(root, ffmpeg)
            output_dir = root / "cli-output"
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workflow_mode": "media_repackage",
                        "job_id": "synthetic-cli",
                        "source_media": str(source),
                        "output_dir": str(output_dir),
                        "output_target": "mkv",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("SUBTITLE_API_KEY", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "media_planning.cli",
                    "--request",
                    str(request_path),
                    "--tool-root",
                    os.environ["MEDIA_REPACKAGE_TOOL_ROOT"],
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            events = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.strip()
            ]
            self.assertEqual("job_completed", events[-1]["event"])
            self.assertEqual("passed", events[-1]["technical_validation"]["status"])
            self.assertTrue(Path(events[-1]["outputs"]["final_media"]).is_file())


if __name__ == "__main__":
    unittest.main()
