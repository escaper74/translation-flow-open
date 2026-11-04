from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from jasna_picture.runner import ExperimentError, _run_streamed
from jasna_picture.workflow import (
    ACCEPTANCE_DURATION_SECONDS,
    BASICVSR_ENGINE_NAMES,
    DEFAULT_SEGMENT_SECONDS,
    PictureWorkflowConfig,
    YOLO_ENGINE_NAME,
    _concat_and_verify,
    _load_or_create_manifest,
    _require_gate,
    _require_decoder,
    _prepare_segments,
    _process_segment,
    _source_video_origin,
    _validate_final,
    _validate_processed_pair,
    build_prepare_command,
    preflight_workflow,
    required_engine_paths,
    run_picture_workflow,
    workflow_plan,
)


def _video_probe(path: str, *, codec: str = "h264", duration: float = 60.0) -> dict:
    return {
        "path": path,
        "format": {"duration": str(duration)},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": codec,
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30000/1001",
            }
        ],
    }


class WorkflowTests(unittest.TestCase):
    def _config(
        self,
        root: Path,
        *,
        scope: str = "acceptance",
        output_name: str = "final.mkv",
    ) -> PictureWorkflowConfig:
        return PictureWorkflowConfig(
            scope=scope,
            source=root / "source.mp4",
            work_dir=root / "work",
            output=root / output_name,
            jasna=root / "Jasna" / "0.10.0" / "jasna.exe",
            ffmpeg=root / "tools" / "ffmpeg.exe",
            ffprobe=root / "tools" / "ffprobe.exe",
            gate_report=root / "gate.json",
            start="01:30:00" if scope == "acceptance" else None,
            segment_seconds=DEFAULT_SEGMENT_SECONDS,
        )

    def test_acceptance_plan_is_two_15_minute_video_only_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))
            command = build_prepare_command(config)
            self.assertEqual(command[command.index("-ss") + 1], "01:30:00")
            self.assertEqual(
                command[command.index("-t") + 1], str(ACCEPTANCE_DURATION_SECONDS)
            )
            self.assertEqual(
                command[command.index("-segment_time") + 1],
                str(DEFAULT_SEGMENT_SECONDS),
            )
            self.assertIn("0:v:0", command)
            self.assertIn("-an", command)
            self.assertIn("-sn", command)
            self.assertIn("-dn", command)
            self.assertIn("copy", command)
            self.assertNotIn("ffv1", [item.casefold() for item in command])

            payload = workflow_plan(config)
            jasna = payload["jasna_segment_command_template"]
            self.assertEqual(jasna[jasna.index("--detection-model") + 1], "lada-yolo-v4")
            self.assertEqual(jasna[jasna.index("--codec") + 1], "hevc")

    def test_full_prepare_has_no_range_cut(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir), scope="full")
            command = build_prepare_command(config)
            self.assertNotIn("-ss", command)
            self.assertNotIn("-t", command)

    def test_full_origin_skips_discard_preroll_packet(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffprobe"],
            0,
            stdout=json.dumps(
                {
                    "packets": [
                        {"pts_time": "-0.033367", "flags": "KD_"},
                        {"pts_time": "0.000000", "flags": "___"},
                    ]
                }
            ),
            stderr="",
        )
        with patch("jasna_picture.workflow.subprocess.run", return_value=completed) as run:
            origin = _source_video_origin(Path("ffprobe.exe"), Path("source.mkv"))
        self.assertEqual(origin["source_origin_seconds"], 0.0)
        self.assertEqual(origin["first_packet_pts_seconds"], -0.033367)
        self.assertEqual(origin["first_packet_flags"], "KD_")
        self.assertEqual(origin["discarded_preroll_packets"], 1)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-select_streams") + 1], "v:0")
        self.assertEqual(command[command.index("-read_intervals") + 1], "%+#64")
        self.assertEqual(command[command.index("-show_entries") + 1], "packet=pts_time,flags")

    def test_full_origin_clamps_small_negative_presentable_packet(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffprobe"],
            0,
            stdout=json.dumps(
                {"packets": [{"pts_time": "-0.033367", "flags": "K__"}]}
            ),
            stderr="",
        )
        with patch("jasna_picture.workflow.subprocess.run", return_value=completed):
            origin = _source_video_origin(Path("ffprobe.exe"), Path("source.mkv"))

        self.assertEqual(0.0, origin["source_origin_seconds"])
        self.assertEqual(-0.033367, origin["first_presentable_packet_pts_seconds"])
        self.assertEqual(
            "NEGATIVE_PRESENTABLE_ORIGIN_CLAMPED",
            origin["informational_warnings"][0]["code"],
        )

    def test_full_origin_rejects_invalid_or_missing_packet_timeline(self) -> None:
        cases = (
            (
                {"packets": [{"pts_time": "not-a-number", "flags": "K__"}]},
                "PTS 不是数值",
            ),
            (
                {"packets": [{"pts_time": "-0.033367", "flags": "KD_"}]},
                "没有可用的非 discard",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                completed = subprocess.CompletedProcess(
                    ["ffprobe"], 0, stdout=json.dumps(payload), stderr=""
                )
                with (
                    patch(
                        "jasna_picture.workflow.subprocess.run",
                        return_value=completed,
                    ),
                    self.assertRaisesRegex(ExperimentError, message),
                ):
                    _source_video_origin(Path("ffprobe.exe"), Path("source.mkv"))

    def test_full_prepare_recovers_verified_unreported_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root, scope="full")
            config.inputs_dir.mkdir(parents=True)
            for index in range(2):
                (config.inputs_dir / f"segment_{index:04d}.mkv").write_bytes(b"segment")
            config.logs_dir.mkdir(parents=True)
            prepare_log = config.logs_dir / "prepare.log"
            prepare_log.write_text(
                "return_code=0\nelapsed_seconds=1.250\n", encoding="utf-8"
            )
            probes = [
                _video_probe("segment-0", duration=10.0),
                _video_probe("segment-1", duration=10.0),
            ]
            origin = {
                "source_origin_seconds": 0.0,
                "first_packet_pts_seconds": -0.033367,
                "first_packet_flags": "KD_",
                "discarded_preroll_packets": 1,
            }
            with (
                patch("jasna_picture.workflow._probe", side_effect=probes),
                patch("jasna_picture.workflow._source_video_origin", return_value=origin),
                patch("jasna_picture.workflow._run_streamed") as run_streamed,
            ):
                records, report = _prepare_segments(config, 20.0)
            run_streamed.assert_not_called()
            self.assertEqual(len(records), 2)
            self.assertTrue(report["reused"])
            self.assertTrue(report["recovered_unreported_segments"])
            self.assertEqual(report["source_origin_seconds"], 0.0)
            self.assertEqual(report["source_origin_evidence"], origin)
            self.assertEqual(report["log"], str(prepare_log))
            persisted = json.loads(config.prepare_report_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted, report)

    def test_unreported_failed_or_incomplete_prepare_is_preserved_then_rebuilt(self):
        for failure in ("failed_log", "gap", "corrupt", "short", "newer_failed_log"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                config = self._config(Path(raw), scope="full")
                config.inputs_dir.mkdir(parents=True)
                names = ["segment_0000.mkv", "segment_0002.mkv" if failure == "gap" else "segment_0001.mkv"]
                for name in names:
                    (config.inputs_dir / name).write_bytes(b"old")
                config.logs_dir.mkdir(parents=True)
                log = config.logs_dir / "prepare.log"
                log.write_text("return_code=" + ("1" if failure == "failed_log" else "0") + "\nelapsed_seconds=1\n")
                if failure == "newer_failed_log":
                    import os
                    os.utime(log, (100, 100))
                    (config.logs_dir / "prepare.attempt02.log").write_text("return_code=1\nelapsed_seconds=1\n")
                def probe(_, path):
                    old = path.read_bytes() == b"old"
                    if old and failure == "corrupt":
                        raise ExperimentError("broken container")
                    return _video_probe(str(path), duration=2 if old and failure == "short" else 10)
                def extract(command, log_path, **kwargs):
                    self.assertEqual([], list(config.inputs_dir.iterdir()))
                    for index in range(2):
                        (config.inputs_dir / f"segment_{index:04d}.mkv").write_bytes(b"new")
                    log_path.write_text("return_code=0\nelapsed_seconds=2\n")
                    return 2.0
                with patch("jasna_picture.workflow._probe", side_effect=probe), patch(
                    "jasna_picture.workflow._source_video_origin", return_value={"source_origin_seconds": 0}
                ), patch("jasna_picture.workflow._run_streamed", side_effect=extract) as run:
                    records, report = _prepare_segments(config, 20)
                    _, reused = _prepare_segments(config, 20)
                self.assertEqual(1, run.call_count)
                self.assertTrue(reused["reused"])
                self.assertFalse(report["reused"])
                self.assertEqual(2, len(records))
                preserved = Path(report["preserved_interrupted_inputs"])
                self.assertEqual(sorted(names), sorted(p.name for p in preserved.iterdir()))
                self.assertTrue(all(p.read_bytes() == b"old" for p in preserved.iterdir()))
                self.assertTrue(report["recovery_reason"])

    def test_prepare_failure_then_retry_completes_without_manual_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw), scope="full")
            attempts = []
            def extract(command, log_path, **kwargs):
                attempts.append(log_path)
                (config.inputs_dir / "segment_0000.mkv").write_bytes(b"partial" if len(attempts) == 1 else b"complete")
                log_path.write_text("return_code=" + ("1" if len(attempts) == 1 else "0") + "\nelapsed_seconds=1\n")
                if len(attempts) == 1:
                    raise ExperimentError("simulated disk full")
                return 1.0
            def probe(_, path):
                return _video_probe(str(path), duration=10 if path.read_bytes() == b"partial" else 30)
            with patch("jasna_picture.workflow._probe", side_effect=probe), patch(
                "jasna_picture.workflow._source_video_origin", return_value={"source_origin_seconds": 0}
            ), patch("jasna_picture.workflow._run_streamed", side_effect=extract):
                with self.assertRaisesRegex(ExperimentError, "disk full"):
                    _prepare_segments(config, 30)
                self.assertFalse(config.prepare_report_path.exists())
                _, report = _prepare_segments(config, 30)
                _, reused = _prepare_segments(config, 30)
            self.assertEqual(2, len(attempts))
            self.assertNotEqual(attempts[0], attempts[1])
            self.assertTrue(reused["reused"])
            self.assertEqual(b"partial", (Path(report["preserved_interrupted_inputs"]) / "segment_0000.mkv").read_bytes())

    def test_invalid_prepare_never_replaces_existing_downstream_work(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw), scope="full")
            config.inputs_dir.mkdir(parents=True)
            segment = config.inputs_dir / "segment_0001.mkv"
            segment.write_bytes(b"partial")
            config.restored_dir.mkdir()
            restored = config.restored_dir / "segment_0000.mkv"
            restored.write_bytes(b"completed picture fixture")
            with patch("jasna_picture.workflow._run_streamed") as run:
                with self.assertRaisesRegex(ExperimentError, "已有画面产物"):
                    _prepare_segments(config, 20)
            run.assert_not_called()
            self.assertEqual(b"partial", segment.read_bytes())
            self.assertEqual(b"completed picture fixture", restored.read_bytes())


    def test_full_workflow_without_history_publishes_only_technical_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(root, scope="full")
            config.source.write_bytes(b"source fixture")
            with (
                patch("jasna_picture.workflow.preflight_workflow", return_value={"source_duration_seconds": 60}),
                patch("jasna_picture.workflow._gpu_utilization_percent", return_value=None),
                patch("jasna_picture.workflow._prepare_segments", return_value=([], {})),
                patch("jasna_picture.workflow._concat_and_verify", return_value={"status": "technical_checks_passed"}),
            ):
                result = run_picture_workflow(config, confirm_gpu_ready=True)
            self.assertIsNone(result["gate_status"])
            report = json.loads(config.final_report_path.read_text(encoding="utf-8"))
            self.assertEqual("candidate_for_human_review", report["release_status"])
            self.assertEqual("pending", report["human_review_status"])
            self.assertFalse(report["current_source_30m_human_reviewed"])
            self.assertNotIn("route_gate", report)

    def test_full_final_report_records_real_mapping_and_seams(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root, scope="full")
            config.source.write_bytes(b"source")
            segment_reports = [
                {
                    "input": str(root / "input-0.mkv"),
                    "output": str(root / "output-0.mkv"),
                    "input_duration_seconds": 10.0,
                    "output_duration_seconds": 10.0,
                    "output_stream": _video_probe("output-0", codec="hevc")["streams"][0],
                },
                {
                    "input": str(root / "input-1.mkv"),
                    "output": str(root / "output-1.mkv"),
                    "input_duration_seconds": 20.0,
                    "output_duration_seconds": 20.0,
                    "output_stream": _video_probe("output-1", codec="hevc")["streams"][0],
                },
            ]
            with (
                patch(
                    "jasna_picture.workflow._probe",
                    return_value=_video_probe("final", codec="hevc", duration=30.0),
                ),
                patch("jasna_picture.workflow._decode_spots", return_value=[]),
            ):
                report = _validate_final(
                    config,
                    root / "final.mkv",
                    segment_reports,
                    30.0,
                    root / "verify.log",
                    0.0,
                )
            self.assertEqual(report["source_origin_seconds"], 0.0)
            self.assertEqual(report["output_seam_positions_seconds"], [10.0])
            self.assertEqual(
                report["segment_mappings"][1]["source_start_seconds"], 10.0
            )
            self.assertEqual(
                report["segment_mappings"][1]["output_start_seconds"], 10.0
            )

    def test_required_engine_list_is_exact(self) -> None:
        jasna = Path(r"C:\Jasna\0.10.0\jasna.exe")
        paths = required_engine_paths(jasna)
        self.assertEqual(len(paths), 7)
        self.assertEqual({path.name for path in paths[:-1]}, set(BASICVSR_ENGINE_NAMES))
        self.assertEqual(paths[-1].name, YOLO_ENGINE_NAME)

    def test_gate_stops_before_touching_missing_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            root.joinpath("gate.json").write_text(
                json.dumps({"status": "technical_checks_passed"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ExperimentError, "--confirm-gpu-ready"):
                run_picture_workflow(config)

    def test_full_does_not_read_missing_or_stale_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw), scope="full")
            with patch("jasna_picture.workflow._load_json", side_effect=AssertionError("ordinary work has no history gate")):
                self.assertEqual({}, _require_gate(config, confirm_gpu_ready=True, confirm_gate_viewed=False))

    def test_acceptance_requires_short_report_and_actual_viewing_confirmation(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(Path(raw))
            config.gate_report.write_text('{"status":"failed"}', encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "短样技术检查没有通过"):
                _require_gate(config, confirm_gpu_ready=True, confirm_gate_viewed=True)
            config.gate_report.write_text('{"status":"technical_checks_passed"}', encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "--confirm-gate-viewed"):
                _require_gate(config, confirm_gpu_ready=True, confirm_gate_viewed=False)
            self.assertEqual("technical_checks_passed", _require_gate(config, confirm_gpu_ready=True, confirm_gate_viewed=True)["status"])

    def test_moved_jasna_keeps_manifest_and_completed_segments(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(root, scope="full")
            config.source.write_bytes(b"source")
            first = _load_or_create_manifest(config)
            config.restored_dir.mkdir()
            completed = config.restored_dir / "segment_0000.mkv"
            completed.write_bytes(b"completed segment")
            before = config.manifest_path.read_bytes()
            moved = replace(config, jasna=root / "new-install/jasna.exe")
            self.assertEqual(first, _load_or_create_manifest(moved))
            self.assertEqual(before, config.manifest_path.read_bytes())
            self.assertEqual(b"completed segment", completed.read_bytes())
            with self.assertRaisesRegex(ExperimentError, "拒绝混用"):
                _load_or_create_manifest(replace(moved, segment_seconds=10))

    def test_manifest_refuses_mixed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.joinpath("source.mp4").write_bytes(b"source")
            config = self._config(root)
            first = _load_or_create_manifest(config)
            self.assertEqual(first["output"], str(config.output.resolve()))
            changed = self._config(root, output_name="different.mkv")
            with self.assertRaisesRegex(ExperimentError, "拒绝混用"):
                _load_or_create_manifest(changed)

    def test_preflight_requires_real_nonempty_engines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root, scope="full")
            for path in (
                config.source,
                config.jasna,
                config.ffmpeg,
                config.ffprobe,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            for engine in required_engine_paths(config.jasna):
                engine.parent.mkdir(parents=True, exist_ok=True)
                engine.write_bytes(b"engine")

            with (
                patch(
                    "jasna_picture.workflow._first_output_line",
                    side_effect=[
                        "jasna 0.10.0",
                        "ffmpeg version 8.1.1",
                        "ffprobe version 8.1.1",
                    ],
                ),
                patch(
                    "jasna_picture.workflow._probe",
                    return_value=_video_probe(str(config.source), duration=7200.0),
                ),
            ):
                result = preflight_workflow(config)
            self.assertEqual(result["status"], "preflight_passed")
            self.assertEqual(len(result["engines"]), 7)


    def test_decoder_evidence_includes_software_upload_without_mislabeling_it(self) -> None:
        fallback = "Using FFmpeg software decoding and uploading frames to CUDA"
        cases = (
            ("Using VALI NVDEC decoder", "vali-nvdec"),
            ("Using PyAV CUDA decoder", "pyav-cuda"),
            (fallback.upper(), "ffmpeg-software-cuda"),
            ("Using VALI NVDEC decoder\n" + fallback, "ffmpeg-software-cuda"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "decoder.log"
            for text, expected in cases:
                with self.subTest(expected=expected, text=text):
                    log.write_text(text, encoding="utf-8")
                    self.assertEqual(_require_decoder(log), expected)
            log.write_text("Processing finished without decoder evidence", encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "解码方式"):
                _require_decoder(log)

    def test_software_decode_segment_completes_and_reuses_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root, scope="full")
            input_path = config.inputs_dir / "segment_0000.mkv"
            input_path.parent.mkdir(parents=True)
            input_path.write_bytes(b"source")
            working = config.restored_dir / "segment_0000.restored.working.mkv"
            output = config.restored_dir / "segment_0000.restored.mkv"
            old_log = config.logs_dir / "process-segment_0000.log"
            old_log.parent.mkdir(parents=True)
            old_log.write_text("old failed attempt\nreturn_code=1\n", encoding="utf-8")
            script = (
                "from pathlib import Path\n"
                "import time\n"
                "print('Using FFmpeg software decoding and uploading frames to CUDA', flush=True)\n"
                "time.sleep(0.2)\n"
                f"Path({str(working)!r}).write_bytes(b'processed')\n"
                "print('Processing video: Processed: 00:10 (300f)', flush=True)\n"
            )

            def probe(_tool, path):
                return _video_probe(str(path), codec="wmv2" if path == input_path else "hevc", duration=10.0)

            with (
                patch("jasna_picture.workflow._probe", side_effect=probe),
                patch("jasna_picture.workflow.build_jasna_command", return_value=[sys.executable, "-c", script]),
            ):
                report = _process_segment(config, input_path, mirror_output=False)
                with patch("jasna_picture.workflow._run_streamed") as rerun:
                    reused = _process_segment(config, input_path, mirror_output=False)
                rerun.assert_not_called()

            self.assertEqual(report["status"], "technical_checks_passed")
            self.assertEqual(report["decoder_backend"], "ffmpeg-software-cuda")
            self.assertEqual(reused["decoder_backend"], "ffmpeg-software-cuda")
            self.assertTrue(reused["reused"])
            self.assertEqual(output.read_bytes(), b"processed")
            self.assertEqual(input_path.read_bytes(), b"source")
            self.assertEqual(old_log.read_text(encoding="utf-8"), "old failed attempt\nreturn_code=1\n")
            self.assertNotEqual(Path(report["log"]), old_log)

    def test_completed_software_decode_media_can_recover_missing_report(self) -> None:
        for working_file in (True, False):
            with self.subTest(working_file=working_file), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = self._config(root, scope="full")
                input_path = config.inputs_dir / "segment_0000.mkv"
                input_path.parent.mkdir(parents=True)
                input_path.write_bytes(b"source")
                output = config.restored_dir / "segment_0000.restored.mkv"
                media = output.with_name("segment_0000.restored.working.mkv") if working_file else output
                media.parent.mkdir(parents=True)
                media.write_bytes(b"complete")
                log = config.logs_dir / "process-segment_0000.log"
                log.parent.mkdir(parents=True)
                log.write_text(
                    "Using FFmpeg software decoding and uploading frames to CUDA\n"
                    "return_code=0\nelapsed_seconds=1.5\n",
                    encoding="utf-8",
                )
                def probe(_tool, path):
                    return _video_probe(str(path), codec="wmv2" if path == input_path else "hevc", duration=10.0)
                with (
                    patch("jasna_picture.workflow._probe", side_effect=probe),
                    patch("jasna_picture.workflow._run_streamed") as rerun,
                ):
                    report = _process_segment(config, input_path, mirror_output=False)
                rerun.assert_not_called()
                self.assertTrue(report["recovered_working_file"])
                self.assertTrue(report["reused"])
                self.assertEqual(report["decoder_backend"], "ffmpeg-software-cuda")
                self.assertEqual(output.read_bytes(), b"complete")

    def test_software_decode_message_does_not_hide_process_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import sys; "
                "print('Using FFmpeg software decoding and uploading frames to CUDA', flush=True); "
                "sys.exit(7)"
            )
            with self.assertRaisesRegex(ExperimentError, "退出码 7"):
                _run_streamed(
                    [sys.executable, "-c", script],
                    Path(temp_dir) / "failed.log",
                    mirror_output=False,
                    timeout_seconds=5,
                )

    def test_processed_pair_must_be_hevc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            with patch(
                "jasna_picture.workflow._probe",
                side_effect=[
                    _video_probe("input.mkv", codec="h264"),
                    _video_probe("output.mkv", codec="h264"),
                ],
            ):
                with self.assertRaisesRegex(ExperimentError, "不是 hevc"):
                    _validate_processed_pair(
                        config,
                        root / "input.mkv",
                        root / "output.mkv",
                    )

    def test_completed_working_segment_is_verified_and_auto_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            input_path = config.inputs_dir / "segment_0000.mkv"
            working = config.restored_dir / "segment_0000.restored.working.mkv"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_bytes(b"input")
            working.parent.mkdir(parents=True, exist_ok=True)
            working.write_bytes(b"complete")
            log = config.logs_dir / "process-segment_0000.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                "Using VALI NVDEC decoder\nreturn_code=0\nelapsed_seconds=12.5\n",
                encoding="utf-8",
            )
            validation = {
                "input": str(input_path),
                "output": str(working),
                "input_duration_seconds": 10.0,
                "output_duration_seconds": 10.0,
                "output_stream": _video_probe("working", codec="hevc")["streams"][0],
            }
            with patch(
                "jasna_picture.workflow._validate_processed_pair",
                return_value=validation,
            ):
                report = _process_segment(config, input_path)

            output = config.restored_dir / "segment_0000.restored.mkv"
            self.assertTrue(output.is_file())
            self.assertFalse(working.exists())
            self.assertTrue(report["recovered_working_file"])
            self.assertTrue(report["reused"])

    def test_unproven_working_segment_is_preserved_before_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            input_path = config.inputs_dir / "segment_0000.mkv"
            working = config.restored_dir / "segment_0000.restored.working.mkv"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_bytes(b"input")
            working.parent.mkdir(parents=True, exist_ok=True)
            working.write_bytes(b"interrupted")

            fresh_validation = {
                "input": str(input_path),
                "output": str(working),
                "input_duration_seconds": 10.0,
                "output_duration_seconds": 10.0,
                "output_stream": _video_probe("working", codec="hevc")["streams"][0],
            }

            def write_fresh_output(*_args, **_kwargs) -> float:
                working.write_bytes(b"fresh")
                return 8.0

            with (
                patch("jasna_picture.workflow._successful_streamed_log", return_value=None),
                patch(
                    "jasna_picture.workflow._probe",
                    return_value=_video_probe(str(input_path), duration=10.0),
                ),
                patch("jasna_picture.workflow._run_streamed", side_effect=write_fresh_output),
                patch(
                    "jasna_picture.workflow._require_decoder",
                    return_value="vali-nvdec",
                ),
                patch(
                    "jasna_picture.workflow._validate_processed_pair",
                    return_value=fresh_validation,
                ),
            ):
                report = _process_segment(config, input_path)

            preserved = Path(report["preserved_interrupted_file"])
            output = config.restored_dir / "segment_0000.restored.mkv"
            self.assertTrue(preserved.is_file())
            self.assertEqual(preserved.read_bytes(), b"interrupted")
            self.assertEqual(output.read_bytes(), b"fresh")
            self.assertFalse(report["reused"])
            self.assertFalse(report["recovered_working_file"])

    def test_completed_final_working_file_is_verified_and_auto_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            restored = config.restored_dir / "segment_0000.restored.mkv"
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(b"segment")
            working = config.output.with_name(
                f"{config.output.stem}.working{config.output.suffix}"
            )
            working.write_bytes(b"complete-final")
            segment_reports = [
                {
                    "output": str(restored),
                    "input_duration_seconds": 10.0,
                    "output_duration_seconds": 10.0,
                }
            ]
            validation = {
                "status": "technical_checks_passed",
                "output": str(working),
                "final_duration_seconds": 10.0,
            }

            with patch(
                "jasna_picture.workflow._validate_final",
                return_value=validation,
            ):
                report = _concat_and_verify(
                    config,
                    segment_reports,
                    10.0,
                    0.0,
                )

            self.assertEqual(config.output.read_bytes(), b"complete-final")
            self.assertFalse(working.exists())
            self.assertTrue(report["reused"])
            self.assertTrue(report["recovered_working_file"])
            self.assertTrue(config.final_report_path.is_file())

    def test_progress_stall_stops_only_after_low_gpu_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            started = time.perf_counter()
            script = (
                "import time; "
                "print('Processing video: Processed: 00:00 (1f)', flush=True); "
                "time.sleep(5)"
            )
            with (
                patch("jasna_picture.runner._gpu_utilization_percent", return_value=0),
                self.assertRaisesRegex(ExperimentError, "没有帧进展"),
            ):
                _run_streamed(
                    [sys.executable, "-c", script],
                    Path(temp_dir) / "stall.log",
                    timeout_seconds=3.0,
                    progress_stall_seconds=0.1,
                )
            self.assertLess(time.perf_counter() - started, 3.0)

    def test_streamed_command_can_log_without_mirroring_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "quiet.log"
            mirrored = io.StringIO()
            with patch("jasna_picture.runner.sys.stdout", mirrored):
                _run_streamed(
                    [sys.executable, "-c", "print('persisted-only')"],
                    log_path,
                    mirror_output=False,
                    timeout_seconds=3.0,
                )

            self.assertEqual("", mirrored.getvalue())
            self.assertIn("persisted-only", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
