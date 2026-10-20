from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from final_mux.workflow import FinalMuxConfig, FinalMuxWorkflow
from translation_flow.srt import SubtitleCue, parse_srt, write_srt


class FinalMuxSyntheticIntegrationTests(unittest.TestCase):
    @staticmethod
    def _tools() -> tuple[Path, Path]:
        tool_root = os.environ.get("FINAL_MUX_TOOL_ROOT")
        if not tool_root:
            raise unittest.SkipTest("FINAL_MUX_TOOL_ROOT is not set")
        candidates = [
            Path(tool_root) / "ffmpeg.exe",
            Path(tool_root)
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
            / "ffmpeg.exe",
        ]
        ffmpeg = next((path for path in candidates if path.is_file()), None)
        if ffmpeg is None:
            raise unittest.SkipTest("FFmpeg was not found under FINAL_MUX_TOOL_ROOT")
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
        )
        if result.returncode != 0:
            raise AssertionError((result.stderr or result.stdout)[-2000:])

    def test_two_audio_tracks_shifted_subtitle_and_video_copy(self) -> None:
        ffmpeg, ffprobe = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mkv"
            processed = root / "processed.mkv"
            subtitle = root / "full.zh.srt"
            output = root / "final.mkv"
            work = root / "work"

            self._run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=25:duration=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=880:sample_rate=48000:duration=6",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map",
                    "2:a:0",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ]
            )
            self._run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=25:duration=3",
                    "-an",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    str(processed),
                ]
            )
            write_srt(
                subtitle,
                [
                    SubtitleCue(1, 1000, 2000, "左边界"),
                    SubtitleCue(2, 2500, 3500, "中间"),
                    SubtitleCue(3, 4250, 5250, "右边界"),
                ],
            )
            config = FinalMuxConfig(
                processed_video=processed,
                source_media=source,
                chinese_srt=subtitle,
                source_start_seconds="1.5",
                output=output,
                work_dir=work,
                seam_positions_seconds=("1.6",),
            )

            report = FinalMuxWorkflow(ffmpeg, ffprobe).run(config)

            self.assertEqual(report["status"], "technical_checks_passed")
            self.assertEqual(report["release_status"], "candidate_for_human_review")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["human_acceptance"], "pending")
            self.assertTrue(output.is_file())
            self.assertEqual(
                parse_srt(config.shifted_srt),
                [
                    SubtitleCue(1, 0, 500, "左边界"),
                    SubtitleCue(2, 1000, 2000, "中间"),
                    SubtitleCue(3, 2750, 3000, "右边界"),
                ],
            )
            payload = json.loads(config.report_path.read_text(encoding="utf-8"))
            checks = payload["technical_validation"]["checks"]
            self.assertTrue(all(checks.values()))
            self.assertEqual(payload["technical_validation"]["source_audio_streams"], 2)
            self.assertEqual(
                payload["technical_validation"]["short_decode_positions_seconds"],
                [0.0, 1.6, 2.0],
            )

    def test_hard_subtitle_picture_delivery_keeps_audio_without_soft_subtitle(self) -> None:
        ffmpeg, ffprobe = self._tools()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mkv"
            processed = root / "processed.mkv"
            output = root / "final.mkv"
            work = root / "work"

            self._run(
                [
                    str(ffmpeg), "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-shortest",
                    str(source),
                ]
            )
            self._run(
                [
                    str(ffmpeg), "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                    "-an", "-c:v", "mpeg4", "-q:v", "5", str(processed),
                ]
            )
            config = FinalMuxConfig(
                processed_video=processed,
                source_media=source,
                chinese_srt=None,
                source_start_seconds="0",
                output=output,
                work_dir=work,
                seam_positions_seconds=("1.5",),
            )

            report = FinalMuxWorkflow(ffmpeg, ffprobe).run(config)

            self.assertEqual(report["status"], "technical_checks_passed")
            self.assertEqual(report["subtitle_mode"], "none")
            self.assertFalse(config.shifted_srt.exists())
            checks = report["technical_validation"]["checks"]
            self.assertTrue(all(checks.values()))
            self.assertTrue(checks["no_subtitle_streams"])


if __name__ == "__main__":
    unittest.main()
