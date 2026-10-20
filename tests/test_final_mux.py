from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from final_mux.workflow import (
    FinalMuxConfig,
    FinalMuxError,
    FinalMuxWorkflow,
    shift_cues_to_window,
)
from translation_flow.srt import SubtitleCue, parse_srt, write_srt


def _completed(args: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def _probe(
    *,
    duration: str = "30.0",
    video: bool = True,
    audio_codecs: tuple[str, ...] = (),
    subtitle: bool = False,
    subtitle_default: int = 1,
) -> dict[str, object]:
    streams: list[dict[str, object]] = []
    if video:
        streams.append(
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "disposition": {"default": 1},
            }
        )
    streams.extend(
        {"codec_type": "audio", "codec_name": codec} for codec in audio_codecs
    )
    if subtitle:
        streams.append(
            {
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "zho"},
                "disposition": {"default": subtitle_default},
            }
        )
    return {
        "format": {"duration": duration, "format_name": "matroska,webm"},
        "streams": streams,
    }


class SubtitleWindowTests(unittest.TestCase):
    def test_clip_shift_reindex_and_keep_text(self) -> None:
        cues = [
            SubtitleCue(10, 500, 1000, "before"),
            SubtitleCue(20, 1000, 1500, "left edge"),
            SubtitleCue(30, 1500, 2500, "inside\nline two"),
            SubtitleCue(40, 2900, 3500, "right edge"),
            SubtitleCue(50, 3000, 4000, "after"),
        ]

        shifted = shift_cues_to_window(cues, 1000, 2000)

        self.assertEqual(
            shifted,
            [
                SubtitleCue(1, 0, 500, "left edge"),
                SubtitleCue(2, 500, 1500, "inside\nline two"),
                SubtitleCue(3, 1900, 2000, "right edge"),
            ],
        )

    def test_exact_non_overlapping_boundaries_are_excluded(self) -> None:
        cues = [
            SubtitleCue(1, 0, 1000, "ends at start"),
            SubtitleCue(2, 3000, 4000, "starts at end"),
        ]
        self.assertEqual([], shift_cues_to_window(cues, 1000, 2000))

    def test_zero_duration_cue_is_not_silently_dropped(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-positive-duration"):
            shift_cues_to_window(
                [SubtitleCue(1, 1000, 1000, "不可呈现")],
                0,
                2000,
            )


class FinalMuxWorkflowTests(unittest.TestCase):
    def _files(self, root: Path) -> tuple[FinalMuxConfig, Path, Path]:
        processed = root / "processed.mkv"
        source = root / "source.mkv"
        subtitle = root / "full.zh.srt"
        ffmpeg = root / "ffmpeg.exe"
        ffprobe = root / "ffprobe.exe"
        for path in (processed, source, ffmpeg, ffprobe):
            path.write_bytes(b"x")
        write_srt(
            subtitle,
            [
                SubtitleCue(7, 9_500, 10_500, "开头"),
                SubtitleCue(8, 20_000, 21_000, "中间"),
                SubtitleCue(9, 39_500, 41_000, "结尾"),
            ],
        )
        config = FinalMuxConfig(
            processed_video=processed,
            source_media=source,
            chinese_srt=subtitle,
            source_start_seconds="10.0",
            output=root / "final.mkv",
            work_dir=root / "work",
            seam_positions_seconds=("15.25",),
        )
        return config, ffmpeg, ffprobe

    def test_plan_uses_explicit_source_window_and_maps_only_required_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, ffmpeg, ffprobe = self._files(root)

            def runner(args: list[str], **_: object):
                if args[-1] == str(config.processed_video):
                    return _completed(args, stdout=json.dumps(_probe(duration="30.0")))
                if args[-1] == str(config.source_media):
                    return _completed(
                        args,
                        stdout=json.dumps(
                            _probe(duration="100.0", audio_codecs=("aac", "ac3"))
                        ),
                    )
                self.fail(f"unexpected call: {args}")

            plan = FinalMuxWorkflow(
                ffmpeg, ffprobe, process_runner=runner
            ).plan(config)

            self.assertEqual(plan["source_start_ms"], 10_000)
            self.assertEqual(plan["selected_source_cue_indices"], [7, 8, 9])
            self.assertEqual(plan["selected_subtitle_cues"], 3)
            self.assertEqual(
                plan["subtitle_window"],
                {
                    "first_source_cue_index": 7,
                    "last_source_cue_index": 9,
                    "first_shifted_start_ms": 0,
                    "first_shifted_end_ms": 500,
                    "last_shifted_start_ms": 29500,
                    "last_shifted_end_ms": 30000,
                },
            )
            command = plan["mux_command"]
            self.assertEqual(
                [command[index + 1] for index, value in enumerate(command) if value == "-map"],
                ["0:v:0", "1:a?", "2:0"],
            )
            self.assertEqual(command[command.index("-c:v") + 1], "copy")
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertEqual(command[command.index("-map_metadata") + 1], "-1")
            self.assertEqual(command[command.index("-disposition:v:0") + 1], "default")
            source_input = command.index(str(config.source_media))
            self.assertEqual(command[source_input - 5 : source_input], ["-ss", "10.0", "-t", "30.0", "-i"])

    def test_plan_without_subtitle_maps_only_video_and_source_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, ffmpeg, ffprobe = self._files(root)
            config = FinalMuxConfig(
                processed_video=config.processed_video,
                source_media=config.source_media,
                chinese_srt=None,
                source_start_seconds=config.source_start_seconds,
                output=config.output,
                work_dir=config.work_dir,
                seam_positions_seconds=config.seam_positions_seconds,
            )

            def runner(args: list[str], **_: object):
                if args[-1] == str(config.processed_video):
                    return _completed(args, stdout=json.dumps(_probe(duration="30.0")))
                if args[-1] == str(config.source_media):
                    return _completed(
                        args,
                        stdout=json.dumps(
                            _probe(duration="100.0", audio_codecs=("aac", "ac3"))
                        ),
                    )
                self.fail(f"unexpected call: {args}")

            plan = FinalMuxWorkflow(
                ffmpeg, ffprobe, process_runner=runner
            ).plan(config)

            self.assertEqual(plan["subtitle_mode"], "none")
            self.assertEqual(plan["selected_subtitle_cues"], 0)
            self.assertIsNone(plan["subtitle_window"])
            self.assertIsNone(plan["shifted_srt"])
            command = plan["mux_command"]
            self.assertEqual(
                [command[index + 1] for index, value in enumerate(command) if value == "-map"],
                ["0:v:0", "1:a?"],
            )
            self.assertNotIn("-c:s", command)

    def test_output_validation_accepts_no_subtitle_when_requested(self) -> None:
        processed = _probe(duration="30.0")
        source = _probe(duration="100.0", audio_codecs=("aac",))
        output = _probe(duration="30.0", audio_codecs=("aac",))
        result = FinalMuxWorkflow._validate_output(
            processed, source, output, expect_subtitle=False
        )
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(result["checks"]["no_subtitle_streams"])

    def test_plan_rejects_zero_duration_input_instead_of_reducing_cue_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, ffmpeg, ffprobe = self._files(root)
            config.chinese_srt.write_text(
                "1\n00:00:10,000 --> 00:00:10,000\n零时长\n\n"
                "2\n00:00:10,000 --> 00:00:11,000\n合法\n",
                encoding="utf-8",
            )

            def runner(args: list[str], **_: object):
                if args[-1] == str(config.processed_video):
                    return _completed(args, stdout=json.dumps(_probe(duration="30.0")))
                if args[-1] == str(config.source_media):
                    return _completed(
                        args,
                        stdout=json.dumps(_probe(duration="100.0", audio_codecs=("aac",))),
                    )
                self.fail(f"unexpected call: {args}")

            with self.assertRaisesRegex(FinalMuxError, "non-positive-duration"):
                FinalMuxWorkflow(ffmpeg, ffprobe, process_runner=runner).plan(config)

    def test_output_validation_rejects_non_default_subtitle(self) -> None:
        processed = _probe(duration="30.0")
        source = _probe(duration="100.0", audio_codecs=("aac", "ac3"))
        output = _probe(
            duration="30.1",
            audio_codecs=("aac", "ac3"),
            subtitle=True,
            subtitle_default=0,
        )
        with self.assertRaisesRegex(FinalMuxError, "subtitle_default"):
            FinalMuxWorkflow._validate_output(processed, source, output)

    def test_failed_validation_never_publishes_formal_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, ffmpeg, ffprobe = self._files(root)
            invalid = [True]

            def runner(args: list[str], **_: object):
                if args[0] == str(ffprobe):
                    target = args[-1]
                    if target == str(config.processed_video):
                        probe = _probe(duration="30.0")
                    elif target == str(config.source_media):
                        probe = _probe(duration="100.0", audio_codecs=("aac",))
                    else:
                        probe = _probe(
                            duration="30.0",
                            audio_codecs=("aac",),
                            subtitle=True,
                            subtitle_default=0 if invalid[0] else 1,
                        )
                    return _completed(args, stdout=json.dumps(probe))
                if "-c:s" in args:
                    Path(args[-1]).write_bytes(b"candidate")
                return _completed(args)

            with self.assertRaisesRegex(FinalMuxError, "subtitle_default"):
                FinalMuxWorkflow(
                    ffmpeg, ffprobe, process_runner=runner
                ).run(config)

            self.assertFalse(config.output.exists())
            self.assertFalse(config.report_path.exists())
            self.assertTrue(config.shifted_srt.exists())
            self.assertEqual(
                [cue.text for cue in parse_srt(config.shifted_srt)],
                ["开头", "中间", "结尾"],
            )
            manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["release_status"], "not_published")
            self.assertEqual(manifest["human_acceptance"], "not_reached")
            invalid[0] = False
            report = FinalMuxWorkflow(ffmpeg, ffprobe, process_runner=runner).run(config)
            self.assertEqual('technical_checks_passed', report['status'])
            self.assertTrue(config.output.is_file())
            self.assertEqual(1, len(list(config.work_dir.glob('failed-attempt-*.json'))))

    def test_output_duration_tolerance_is_bounded(self) -> None:
        processed = _probe(duration="30.0")
        source = _probe(duration="100.0", audio_codecs=("aac",))
        output = _probe(duration="30.751", audio_codecs=("aac",), subtitle=True)
        result = FinalMuxWorkflow._validate_output(processed, source, output)
        self.assertTrue(result["checks"]["duration_close_to_processed"])
        self.assertEqual(1, len(result["warnings"]))

        output = _probe(duration="32.1", audio_codecs=("aac",), subtitle=True)
        with self.assertRaisesRegex(FinalMuxError, "duration_close_to_processed"):
            FinalMuxWorkflow._validate_output(processed, source, output)


if __name__ == "__main__":
    unittest.main()
