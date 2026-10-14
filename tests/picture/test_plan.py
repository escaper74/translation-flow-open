from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest

from jasna_picture.plan import ExperimentPaths, build_experiment_plan
from jasna_picture.runner import (
    ExperimentError,
    _run_streamed,
    execute_step,
    has_software_decode_fallback,
)


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = ExperimentPaths(
            source=Path(r"E:\source\movie.mp4"),
            work_dir=Path(r"E:\jasna_tests\case"),
            jasna=Path(r"C:\Jasna\jasna.exe"),
            ffmpeg=Path(r"C:\Tools\ffmpeg.exe"),
            ffprobe=Path(r"C:\Tools\ffprobe.exe"),
        )
        self.plan = build_experiment_plan(self.paths)

    def test_step_order_is_explicit(self) -> None:
        self.assertEqual(
            [step.key for step in self.plan.steps],
            [
                "prepare-short",
                "process-short",
                "verify-short",
                "prepare-long",
                "process-long-a",
                "process-long-b",
                "concat-long",
                "verify-long",
            ],
        )

    def test_extracts_are_video_only_stream_copy(self) -> None:
        for key in ("prepare-short", "prepare-long"):
            command = self.plan.step(key).command
            assert command is not None
            self.assertIn("-map", command)
            self.assertIn("0:v:0", command)
            self.assertIn("-an", command)
            self.assertIn("-sn", command)
            self.assertIn("-dn", command)
            self.assertIn("copy", command)
            self.assertNotIn("ffv1", [item.casefold() for item in command])

    def test_long_extract_uses_one_continuous_selection_and_one_split(self) -> None:
        command = self.plan.step("prepare-long").command
        assert command is not None
        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(command[command.index("-segment_times") + 1], "900")
        self.assertEqual(command[command.index("-t") + 1], "1800")

    def test_jasna_baseline_is_explicit_and_direct_to_final_codec(self) -> None:
        command = self.plan.step("process-short").command
        assert command is not None
        expected_pairs = {
            "--max-clip-size": "90",
            "--temporal-overlap": "8",
            "--detection-model": "lada-yolo-v4",
            "--max-detection-gap": "2",
            "--min-detection-duration": "2",
            "--secondary-restoration": "none",
            "--denoise": "none",
            "--vr-mode": "off",
            "--codec": "hevc",
            "--cq": "28",
            "--post-export-action": "none",
            "--log-level": "info",
        }
        for option, expected in expected_pairs.items():
            self.assertEqual(command[command.index(option) + 1], expected)
        self.assertIn("--fp16", command)
        self.assertIn("--compile-basicvsrpp", command)
        self.assertIn("--enable-crossfade", command)
        self.assertIn("--scene-detection", command)
        self.assertNotIn("--encoder-settings", command)
        self.assertNotIn("ffv1", [item.casefold() for item in command])

    def test_concat_is_stream_copy_and_video_only(self) -> None:
        command = self.plan.step("concat-long").command
        assert command is not None
        self.assertIn("concat", command)
        self.assertIn("copy", command)
        self.assertIn("-an", command)
        self.assertIn("-sn", command)
        self.assertNotIn("ffv1", [item.casefold() for item in command])

    def test_gpu_step_requires_explicit_confirmation_before_touching_tools(self) -> None:
        with self.assertRaisesRegex(ExperimentError, "--confirm-gpu-ready"):
            execute_step(self.plan, "process-short", confirm_gpu_ready=False)

    def test_gpu_step_requires_cached_engines_before_touching_tools(self) -> None:
        with self.assertRaisesRegex(ExperimentError, "--confirm-engines-ready"):
            execute_step(
                self.plan,
                "process-short",
                confirm_gpu_ready=True,
                confirm_engines_ready=False,
            )

    def test_long_stage_requires_short_technical_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = ExperimentPaths(
                source=self.paths.source,
                work_dir=Path(temp_dir),
                jasna=self.paths.jasna,
                ffmpeg=self.paths.ffmpeg,
                ffprobe=self.paths.ffprobe,
            )
            plan = build_experiment_plan(paths)
            with self.assertRaisesRegex(ExperimentError, "短样技术检查报告"):
                execute_step(plan, "prepare-long", confirm_short_viewed=True)

    def test_software_decode_fallback_is_case_insensitive(self) -> None:
        self.assertTrue(
            has_software_decode_fallback(
                "Warning: Using FFmpeg software decoding and uploading frames to CUDA"
            )
        )
        self.assertFalse(has_software_decode_fallback("Using VALI NVDEC decoder"))

    def test_streamed_command_timeout_stops_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            started = time.perf_counter()
            with self.assertRaisesRegex(ExperimentError, "墙钟超过"):
                _run_streamed(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    Path(temp_dir) / "timeout.log",
                    timeout_seconds=0.1,
                )
            self.assertLess(time.perf_counter() - started, 3.0)

    def test_plan_serialization_contains_commands_but_does_not_require_files(self) -> None:
        payload = self.plan.as_dict()
        self.assertEqual(payload["baseline"]["jasna_release"], "v0.10.0")
        self.assertEqual(payload["ranges"]["short"]["requested_seconds"], 60)
        self.assertIn("windows_command_line", payload["steps"][0])


if __name__ == "__main__":
    unittest.main()
