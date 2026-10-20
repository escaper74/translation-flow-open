from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from video_pipeline.config import (
    CONCEPTUAL_STAGE_ORDER,
    QUALITY_DENOISE_FILTER,
    QUALITY_FINAL_CAS_FILTER,
    PipelineOptions,
    build_execution_plan,
)
from video_pipeline.events import EventEmitter
from video_pipeline.runner import PipelineError, PipelineRequest, VideoPipelineRunner
from video_pipeline.safety import UnsafePathError, ensure_within, safe_remove_tree
from video_pipeline.state import JobState, ResumeMismatchError, canonical_hash


class ExecutionPlanTests(unittest.TestCase):
    def test_all_eight_option_combinations_have_one_canonical_order(self) -> None:
        for values in itertools.product((False, True), repeat=3):
            with self.subTest(values=values):
                options = PipelineOptions(*values)
                plan = build_execution_plan(options)
                stages = [item["stage"] for item in plan]
                positions = [
                    CONCEPTUAL_STAGE_ORDER.index(stage)
                    for stage in stages
                    if stage in CONCEPTUAL_STAGE_ORDER
                ]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(stages[-1], "technical_validation")
                if any(values):
                    self.assertIn("final_encode_and_audio_mux", stages)
                    self.assertNotIn("passthrough_remux", stages)
                else:
                    self.assertIn("passthrough_remux", stages)

    def test_three_enabled_reproduce_candidate_07_method_semantics(self) -> None:
        plan = build_execution_plan(PipelineOptions(True, True, True))
        self.assertEqual(
            [item["stage"] for item in plan],
            [
                "demosaic",
                "quality_enhancement",
                "frame_interpolation_2x",
                "final_encode_and_audio_mux",
                "technical_validation",
            ],
        )
        quality = plan[1]
        self.assertEqual(quality["denoise_filter"], QUALITY_DENOISE_FILTER)
        self.assertEqual(quality["final_filter"], QUALITY_FINAL_CAS_FILTER)
        self.assertIsNone(quality["resolution_scale"])
        self.assertEqual(plan[2]["model"], "rife-v4.6")
        self.assertEqual(plan[3]["preset"], "veryfast")
        self.assertEqual(plan[3]["crf"], 12)


class StateAndEventTests(unittest.TestCase):
    def test_resume_requires_identical_cache_key_and_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job.json"
            request = {"input": "a", "options": {"x": True}}
            key = canonical_hash(request)
            first, resumed = JobState.create_or_load(
                path, job_id="job-1", cache_key=key, request=request
            )
            self.assertFalse(resumed)
            first.set_status("running")
            loaded, resumed = JobState.create_or_load(
                path, job_id="ignored", cache_key=key, request=request
            )
            self.assertTrue(resumed)
            self.assertEqual(loaded.data["job_id"], "job-1")
            with self.assertRaises(ResumeMismatchError):
                JobState.create_or_load(
                    path,
                    job_id="ignored",
                    cache_key=canonical_hash({"changed": True}),
                    request={"changed": True},
                )

    def test_events_are_utf8_json_lines_on_stdout_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            path = Path(temp_dir) / "events.jsonl"
            emitter = EventEmitter(path, output)
            emitter.emit("stage_progress", stage="去马赛克", percent=50)
            stdout_item = json.loads(output.getvalue())
            disk_item = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stdout_item, disk_item)
            self.assertEqual(disk_item["stage"], "去马赛克")


class PathSafetyTests(unittest.TestCase):
    def test_cleanup_refuses_paths_outside_job_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job = root / "job"
            job.mkdir()
            outside = root / "outside"
            outside.mkdir()
            with self.assertRaises(UnsafePathError):
                ensure_within(outside, job)
            with self.assertRaises(UnsafePathError):
                safe_remove_tree(job, job)
            self.assertTrue(outside.exists())
            self.assertTrue(job.exists())

    def test_pipeline_refuses_existing_output_before_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            output = root / "output.mkv"
            source.write_bytes(b"source")
            output.write_bytes(b"existing")
            request = PipelineRequest(
                input_video=source,
                output_video=output,
                options=PipelineOptions(False, False, False),
                tool_root=root / "missing-tools",
                job_dir=root / "job",
            )
            with self.assertRaises(FileExistsError):
                VideoPipelineRunner(request, stdout=io.StringIO()).run()
            self.assertEqual(output.read_bytes(), b"existing")

    def test_owned_job_directory_can_retry_after_early_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            source.write_bytes(b"not-media-yet")
            job = root / "job"
            request = PipelineRequest(
                input_video=source,
                output_video=root / "output.mkv",
                options=PipelineOptions(False, False, False),
                tool_root=root / "missing-tools",
                job_dir=job,
            )
            for _ in range(2):
                stdout = io.StringIO()
                with self.assertRaises(FileNotFoundError):
                    VideoPipelineRunner(request, stdout=stdout).run()
                events = [json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(events[-1]["event"], "job_failed")
            self.assertTrue((job / ".video-pipeline-owned").is_file())
            self.assertFalse((job / "job.json").exists())

    def test_job_lock_rejects_concurrent_start_and_preserves_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            request = PipelineRequest(
                input_video=source,
                output_video=root / "output.mkv",
                options=PipelineOptions(False, False, False),
                tool_root=root / "tools",
                job_dir=root / "job",
            )
            first = VideoPipelineRunner(request, stdout=io.StringIO())
            first._prepare_job_directory()
            first._acquire_job_lock()
            try:
                second = VideoPipelineRunner(request, stdout=io.StringIO())
                second._prepare_job_directory()
                with self.assertRaises(PipelineError):
                    second._acquire_job_lock()
            finally:
                first._release_job_lock()

            stale = first.lock_path
            stale.write_text(
                json.dumps({"pid": 999999999, "token": "stale"}),
                encoding="utf-8",
            )
            recovered = VideoPipelineRunner(request, stdout=io.StringIO())
            recovered._prepare_job_directory()
            recovered._acquire_job_lock()
            try:
                self.assertTrue(
                    list(recovered.job_dir.glob(".video-pipeline-running.lock.stale-*.json"))
                )
            finally:
                recovered._release_job_lock()


@unittest.skipUnless(
    os.environ.get("VIDEO_PIPELINE_TOOL_ROOT"),
    "set VIDEO_PIPELINE_TOOL_ROOT for local FFmpeg synthetic integration",
)
class SyntheticPipelineIntegrationTests(unittest.TestCase):
    tool_root = Path(os.environ.get("VIDEO_PIPELINE_TOOL_ROOT", "."))
    ffmpeg = (
        tool_root
        / "ffmpeg"
        / "ffmpeg-8.1.1-essentials_build"
        / "bin"
        / "ffmpeg.exe"
    )

    def _make_source(self, root: Path) -> Path:
        subtitle = root / "source.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,400\n测试字幕\n",
            encoding="utf-8",
        )
        source = root / "source.mkv"
        subprocess.run(
            [
                str(self.ffmpeg),
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                (
                    "color=c=0x305080:s=320x180:r=30000/1001:d=0.5,"
                    "drawbox=x='mod(t*60,280)':y=70:w=40:h=40:color=0xff7040:t=fill"
                ),
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000:duration=0.5",
                "-i",
                str(subtitle),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:s:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                str(source),
            ],
            check=True,
        )
        return source

    def test_passthrough_remux_is_resumable_and_omits_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._make_source(root)
            output = root / "画面直通.mkv"
            job = root / "job"
            request = PipelineRequest(
                input_video=source,
                output_video=output,
                options=PipelineOptions(False, False, False),
                tool_root=self.tool_root,
                job_dir=job,
                cleanup_on_success=True,
            )
            first_events = io.StringIO()
            VideoPipelineRunner(request, stdout=first_events).run()
            self.assertTrue(output.is_file())
            report = json.loads((job / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["checks"]["subtitles_omitted"]["ok"])
            second_events = io.StringIO()
            VideoPipelineRunner(request, stdout=second_events).run()
            completed = [
                json.loads(line)
                for line in second_events.getvalue().splitlines()
                if json.loads(line)["event"] == "job_completed"
            ]
            self.assertTrue(completed[-1]["reused"])

    def test_quality_only_synthetic_run_keeps_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._make_source(root)
            output = root / "画质增强.mkv"
            job = root / "quality-job"
            request = PipelineRequest(
                input_video=source,
                output_video=output,
                options=PipelineOptions(False, True, False),
                tool_root=self.tool_root,
                job_dir=job,
                cleanup_on_success=True,
            )
            VideoPipelineRunner(request, stdout=io.StringIO()).run()
            report = json.loads((job / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["spec"]["source_resolution"], "320x180")
            self.assertEqual(report["spec"]["output_resolution"], "320x180")

    @unittest.skipUnless(
        os.environ.get("VIDEO_PIPELINE_RUN_GPU_TESTS") == "1",
        "set VIDEO_PIPELINE_RUN_GPU_TESTS=1 for the slow Lada/RIFE integration",
    )
    def test_all_enabled_gpu_synthetic_run_uses_formal_07_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._make_source(root)
            output = root / "三项全开.mkv"
            job = root / "all-enabled-job"
            request = PipelineRequest(
                input_video=source,
                output_video=output,
                options=PipelineOptions(True, True, True),
                tool_root=self.tool_root,
                job_dir=job,
                cleanup_on_success=True,
                rife_chunk_frames=30,
            )
            VideoPipelineRunner(request, stdout=io.StringIO()).run()
            report = json.loads((job / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["spec"]["source_resolution"], "320x180")
            self.assertEqual(report["spec"]["output_resolution"], "320x180")
            self.assertEqual(report["spec"]["expected_frame_rate"], "60000/1001")
            self.assertLess(
                abs(
                    float(
                        Fraction(report["spec"]["output_frame_rate"])
                        - Fraction(60000, 1001)
                    )
                ),
                0.001,
            )
            self.assertEqual(report["spec"]["adjacent_exact_duplicate_frames"], 0)


if __name__ == "__main__":
    unittest.main()
