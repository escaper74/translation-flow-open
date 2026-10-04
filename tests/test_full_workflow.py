from __future__ import annotations

import json
from io import StringIO
import os
from pathlib import Path
import tempfile
import threading
import unittest

from full_workflow.contracts import FullWorkflowRequest
from full_workflow.runner import FullWorkflowRunner
from translation_flow.events import EventEmitter
from translation_flow.srt import parse_srt, subtitle_digest


def _request(root: Path, *, additional_mp4: bool | None = None) -> FullWorkflowRequest:
    source = root / "movie.mkv"
    if not source.exists():
        source.write_bytes(b"source")
    payload = {
        "schema_version": 1,
        "workflow_mode": "translation_picture_final",
        "job_id": "job-full-offline",
        "input_video": str(source),
        "output_dir": str(root / "out"),
        "profile_id": "adult_direct",
        "provider": {
            "id": "deepseek",
            "host": "api.deepseek.com",
            "model": "deepseek-v4-pro",
        },
        "consent": {
            "current_run": True,
            "subtitle_text_only": True,
            "audio_video_local": True,
        },
        "gates": {"picture_and_mux_30m_human_accepted": True},
    }
    if additional_mp4 is not None:
        payload["delivery"] = {"additional_mp4": additional_mp4}
    return FullWorkflowRequest.from_mapping(payload)


class _FakeTranslationRunner:
    def __init__(
        self,
        request,
        api_key,
        *,
        calls,
        after_asr_ready=None,
        **kwargs,
    ) -> None:
        self.request = request
        self.api_key = api_key
        self.calls = calls
        self.emitter = kwargs["emitter"]
        self.on_asr_ready = kwargs["on_asr_ready"]
        self.after_asr_ready = after_asr_ready

    def run(self):
        self.calls.append("translation")
        if os.environ.get("SUBTITLE_API_KEY"):
            raise AssertionError("credential must be popped before business stages")
        self.emitter.emit(
            "job_started",
            profile_id=self.request.profile_id,
            input_video=str(self.request.input_video),
        )
        self.emitter.emit(
            "stage_progress",
            stage="asr_a",
            overall_percent=25,
            detail="synthetic nested translation event",
        )
        self.on_asr_ready()
        if self.after_asr_ready is not None:
            self.after_asr_ready()
        job_root = self.request.output_dir / ".translation-flow" / self.request.job_id
        job_root.mkdir(parents=True, exist_ok=True)
        subtitle = self.request.output_dir / "movie.zh-CN.srt"
        original = self.request.output_dir / "movie.original.zh-CN.mkv"
        technical = job_root / "technical-validation.report.json"
        translation_report = job_root / "translation.report.json"
        fusion = job_root / "asr" / "fusion" / "fusion.ja.srt"
        self.request.output_dir.mkdir(parents=True, exist_ok=True)
        fusion.parent.mkdir(parents=True, exist_ok=True)
        fusion.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n日本語\n",
            encoding="utf-8",
        )
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n中文\n",
            encoding="utf-8",
        )
        original.write_bytes(b"original")
        translation_report.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "profile": self.request.profile_id,
                    "provider": self.request.provider.id,
                    "model": self.request.provider.model,
                    "output": str(subtitle),
                    "source_sha256": subtitle_digest(parse_srt(fusion)),
                    "subtitle_count": 1,
                    "translated_count": 1,
                }
            ),
            encoding="utf-8",
        )
        technical.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "source": str(self.request.input_video),
                    "chinese_srt": str(subtitle),
                    "output": str(original),
                    "input_subtitle_cues": 1,
                }
            ),
            encoding="utf-8",
        )
        stat = self.request.input_video.stat()
        result = {
            "outputs": {
                "chinese_srt": str(subtitle),
                "final_mkv": str(original),
                "technical_report": str(technical),
            },
            "technical_validation": {
                "status": "passed",
                "report_path": str(technical),
            },
            "asr": {
                "source": str(self.request.input_video),
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "fusion_srt": str(fusion),
                "fusion_cues": 1,
                "structure_warnings": {
                    "A": ["synthetic ASR presentation warning"],
                    "C": [],
                    "fusion": [],
                },
            },
        }
        (job_root / "job.result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        self.emitter.emit("job_completed", **result)
        return result


class _FakeFinalMux:
    def __init__(self, ffmpeg, ffprobe, *, calls, configs) -> None:
        self.calls = calls
        self.configs = configs

    def run(self, config):
        self.calls.append("final_mux")
        self.configs.append(config)
        config.output.parent.mkdir(parents=True, exist_ok=True)
        config.work_dir.mkdir(parents=True, exist_ok=True)
        config.output.write_bytes(b"final")
        report = {
            "status": "technical_checks_passed",
            "input_lineage": {
                "processed_video": str(config.processed_video.resolve()),
                "source_media": str(config.source_media.resolve()),
                "full_timeline_chinese_srt": str(config.chinese_srt.resolve()),
            },
            "source_start_seconds": config.source_start_seconds,
            "seam_positions_seconds": [
                float(value) for value in config.seam_positions_seconds
            ],
            "output": str(config.output.resolve()),
            "technical_validation": {
                "checks": {"synthetic": True},
                "warnings": ["synthetic final container warning"],
            },
        }
        config.report_path.write_text(json.dumps(report), encoding="utf-8")
        return report


class _FakeMediaDelivery:
    def __init__(self, request, *, calls, fail=False, **kwargs) -> None:
        self.request = request
        self.calls = calls
        self.fail = fail

    def run(self):
        self.calls.append("mp4_delivery")
        if self.fail:
            raise RuntimeError("synthetic MP4 delivery failure")
        self.request.output_dir.mkdir(parents=True, exist_ok=True)
        self.request.job_root.mkdir(parents=True, exist_ok=True)
        self.request.output_path.write_bytes(b"mp4")
        source_stat = self.request.source_media.stat()
        report = {
            "status": "technical_checks_passed",
            "source": {
                "path": str(self.request.source_media.resolve()),
                "size_bytes": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            },
            "output": {
                "path": str(self.request.output_path),
                "target": "mp4",
            },
            "technical_validation": {"checks": {"synthetic": True}},
        }
        self.request.technical_report_path.write_text(
            json.dumps(report), encoding="utf-8"
        )
        return {
            "outputs": {
                "final_media": str(self.request.output_path),
                "technical_report": str(self.request.technical_report_path),
            }
        }


class FullWorkflowTests(unittest.TestCase):
    def _runner(
        self,
        root: Path,
        calls: list[str],
        configs: list[object],
        *,
        additional_mp4: bool = False,
        media_fails: bool = False,
    ):
        request = _request(root, additional_mp4=additional_mp4)

        def translation_factory(*args, **kwargs):
            return _FakeTranslationRunner(*args, calls=calls, **kwargs)

        def picture_runner(
            config,
            *,
            confirm_gpu_ready,
            confirm_gate_viewed,
            mirror_output,
            cancelled=None,
        ):
            calls.append("picture")
            self.assertTrue(confirm_gpu_ready)
            self.assertTrue(confirm_gate_viewed)
            self.assertFalse(mirror_output)
            config.output.parent.mkdir(parents=True, exist_ok=True)
            config.output.write_bytes(b"picture")
            stat = config.source.stat()
            report_path = config.work_dir / "reports" / "workflow-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                "status": "technical_checks_passed",
                "scope": "full",
                "output": str(config.output),
                "source_identity": {
                    "path": str(config.source.resolve()),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                },
                "source_origin_seconds": 0.0,
                "segment_mappings": [
                    {
                        "index": 0,
                        "source_start_seconds": 0.0,
                        "source_end_seconds": 912.345,
                        "output_start_seconds": 0.0,
                        "output_end_seconds": 912.345,
                    },
                    {
                        "index": 1,
                        "source_start_seconds": 912.345,
                        "source_end_seconds": 1800.0,
                        "output_start_seconds": 912.345,
                        "output_end_seconds": 1800.0,
                    },
                ],
                "output_seam_positions_seconds": [912.345],
                "final_duration_seconds": 1800.0,
                "route": {
                    "detector": "lada-yolo-v4",
                    "restorer": "basicvsrpp",
                    "codec": "hevc",
                },
                "informational_warnings": [
                    {
                        "code": "SYNTHETIC_PICTURE_WARNING",
                        "message": "synthetic picture timeline warning",
                    }
                ],
                "report": str(report_path),
            }
            report_path.write_text(json.dumps(result), encoding="utf-8")
            return result

        def final_factory(*args, **kwargs):
            return _FakeFinalMux(*args, calls=calls, configs=configs, **kwargs)

        def media_factory(*args, **kwargs):
            return _FakeMediaDelivery(
                *args, calls=calls, fail=media_fails, **kwargs
            )

        return FullWorkflowRunner(
            request,
            "one-run-secret",
            tool_root=root / "tools",
            jasna=root / "jasna.exe",
            acceptance_gate_report=root / "accepted-30m.json",
            translation_runner_factory=translation_factory,
            picture_runner=picture_runner,
            final_mux_factory=final_factory,
            media_delivery_factory=media_factory,
        )

    def test_delivery_defaults_to_no_additional_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            request = _request(Path(raw))
            self.assertFalse(request.delivery.additional_mp4)

    def test_delivery_rejects_non_boolean_additional_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "movie.mkv"
            source.write_bytes(b"source")
            payload = {
                "schema_version": 1,
                "workflow_mode": "translation_picture_final",
                "job_id": "job-invalid-delivery",
                "input_video": str(source),
                "output_dir": str(root / "out"),
                "profile_id": "normal",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": "deepseek-v4-pro",
                },
                "consent": {
                    "current_run": True,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
                "gates": {"picture_and_mux_30m_human_accepted": True},
                "delivery": {"additional_mp4": "yes"},
            }
            with self.assertRaisesRegex(ValueError, "additional_mp4"):
                FullWorkflowRequest.from_mapping(payload)

    def test_parallel_branches_gates_and_final_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            os.environ["SUBTITLE_API_KEY"] = "must-not-leak"
            runner = self._runner(root, calls, configs)
            result = runner.run()

            self.assertEqual(calls, ["translation", "picture", "final_mux"])
            self.assertNotIn("SUBTITLE_API_KEY", os.environ)
            self.assertEqual(result["status"], "technical_checks_passed")
            self.assertEqual(result["human_review_status"], "pending")
            self.assertEqual(result["release_status"], "candidate_for_human_review")
            self.assertEqual(
                result["warnings"],
                [
                    "ASR A: synthetic ASR presentation warning",
                    "synthetic picture timeline warning",
                    "synthetic final container warning",
                ],
            )
            config = configs[0]
            self.assertEqual(config.source_start_seconds, "0.0")
            self.assertEqual(config.seam_positions_seconds, ("912.345",))
            self.assertTrue(str(config.chinese_srt).endswith("movie.zh-CN.srt"))
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())
            manifest_text = runner.paths.manifest.read_text(encoding="utf-8")
            self.assertNotIn("one-run-secret", manifest_text)

    def test_picture_report_accepts_small_rounding_but_rejects_content_loss(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(root, calls, configs)
            runner.run()
            report_path = runner.paths.picture_work_dir / "reports" / "workflow-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))

            report["source_origin_seconds"] = 0.04
            report["segment_mappings"][1]["source_start_seconds"] = 912.385
            report["segment_mappings"][1]["output_start_seconds"] = 912.385
            report["output_seam_positions_seconds"] = [912.385]
            report["final_duration_seconds"] = 1801.9
            runner._validate_picture_result(report)

            report["final_duration_seconds"] = 1802.1
            with self.assertRaisesRegex(ValueError, "接缝或成品时长"):
                runner._validate_picture_result(report)

    def test_optional_mp4_delivery_runs_after_final_mkv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(
                root, calls, configs, additional_mp4=True
            )

            result = runner.run()

            self.assertEqual(
                calls, ["translation", "picture", "final_mux", "mp4_delivery"]
            )
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())
            self.assertTrue(Path(result["outputs"]["final_mp4"]).is_file())
            self.assertEqual(
                result["stages"]["mp4_delivery"]["status"],
                "technical_checks_passed",
            )
            self.assertFalse(result["stages"]["mp4_delivery"]["reused"])
            manifest = json.loads(runner.paths.manifest.read_text(encoding="utf-8"))
            self.assertNotIn("delivery", manifest)

    def test_optional_mp4_delivery_reuses_existing_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs, additional_mp4=True)
            first.run()
            calls.clear()
            configs.clear()

            resumed = self._runner(root, calls, configs, additional_mp4=True)
            result = resumed.run()

            self.assertEqual(calls, ["picture"])
            self.assertTrue(result["stages"]["mp4_delivery"]["reused"])

    def test_optional_mp4_reuse_rejects_wrong_source_lineage_but_keeps_mkv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs, additional_mp4=True)
            first.run()
            mp4_request = first._mp4_delivery_request()
            report = json.loads(
                mp4_request.technical_report_path.read_text(encoding="utf-8")
            )
            report["source"]["size_bytes"] = -1
            mp4_request.technical_report_path.write_text(
                json.dumps(report), encoding="utf-8"
            )
            calls.clear()
            configs.clear()

            resumed = self._runner(root, calls, configs, additional_mp4=True)
            result = resumed.run()

            self.assertEqual(calls, ["picture"])
            self.assertEqual(result["status"], "technical_checks_passed")
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())
            self.assertEqual(result["stages"]["mp4_delivery"]["status"], "failed")
            self.assertIn("源谱系", result["stages"]["mp4_delivery"]["error"])

    def test_optional_mp4_failure_preserves_successful_mkv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(
                root,
                calls,
                configs,
                additional_mp4=True,
                media_fails=True,
            )

            result = runner.run()

            self.assertEqual(result["status"], "technical_checks_passed")
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())
            self.assertNotIn("final_mp4", result["outputs"])
            self.assertEqual(result["stages"]["mp4_delivery"]["status"], "failed")
            self.assertIn(
                "synthetic MP4 delivery failure",
                result["stages"]["mp4_delivery"]["error"],
            )
            manifest = json.loads(runner.paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "technical_checks_passed")

    def test_picture_starts_before_translation_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            picture_started = threading.Event()
            runner = self._runner(root, calls, configs)
            original_picture = runner.picture_runner

            def after_asr_ready() -> None:
                self.assertTrue(picture_started.wait(2), "picture branch did not overlap")
                calls.append("translation_after_picture_started")

            def translation_factory(*args, **kwargs):
                return _FakeTranslationRunner(
                    *args,
                    calls=calls,
                    after_asr_ready=after_asr_ready,
                    **kwargs,
                )

            def picture_runner(*args, **kwargs):
                calls.append("picture_started")
                picture_started.set()
                return original_picture(*args, **kwargs)

            runner.translation_runner_factory = translation_factory
            runner.picture_runner = picture_runner
            runner.run()

            self.assertLess(
                calls.index("picture_started"),
                calls.index("translation_after_picture_started"),
            )
            self.assertEqual("final_mux", calls[-1])

    def test_resume_reuses_translation_and_final_reports(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs)
            first.run()
            calls.clear()
            configs.clear()

            second = self._runner(root, calls, configs)
            result = second.run()

            self.assertEqual(calls, ["picture"])
            self.assertTrue(result["stages"]["translation"]["reused"])
            self.assertTrue(result["stages"]["final_mux"]["reused"])

    def test_legacy_serial_manifest_can_resume_under_parallel_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(root, calls, configs)
            legacy = runner._manifest_payload()
            legacy["gates"].pop("picture_gpu_after_asr_fusion")
            legacy["gates"]["picture_gpu_after_translation_completion"] = True
            runner.paths.workflow_root.mkdir(parents=True, exist_ok=True)
            runner.paths.manifest.write_text(json.dumps(legacy), encoding="utf-8")

            runner.run()

            current = json.loads(runner.paths.manifest.read_text(encoding="utf-8"))
            self.assertTrue(current["gates"]["picture_gpu_after_asr_fusion"])
            self.assertNotIn(
                "picture_gpu_after_translation_completion", current["gates"]
            )

    def test_resume_with_completed_translation_does_not_require_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs)
            first.run()
            calls.clear()
            configs.clear()

            resumed = self._runner(root, calls, configs)
            resumed.api_key = ""
            result = resumed.run()

            self.assertEqual(calls, ["picture"])
            self.assertTrue(result["stages"]["translation"]["reused"])

    def test_missing_api_key_still_fails_before_new_translation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(root, calls, configs)
            runner.api_key = ""

            with self.assertRaisesRegex(ValueError, "没有可复用的正式翻译结果"):
                runner.run()
            self.assertEqual(calls, [])

    def test_empty_api_key_rejects_invalid_translation_before_any_branch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs)
            first.run()
            result = json.loads(
                first._translation_result_path().read_text(encoding="utf-8")
            )
            Path(result["outputs"]["chinese_srt"]).write_text(
                "1\n00:00:00,500 --> 00:00:00,500\n中文\n",
                encoding="utf-8",
            )
            calls.clear()
            configs.clear()

            resumed = self._runner(root, calls, configs)
            resumed.api_key = ""
            with self.assertRaisesRegex(
                ValueError,
                "缺少本次 DeepSeek API Key.*未通过谱系验证.*无法复用",
            ):
                resumed.run()

            self.assertEqual(calls, [])

    def test_resume_rejects_zero_duration_translation_before_picture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs)
            first.run()
            result = json.loads(
                first._translation_result_path().read_text(encoding="utf-8")
            )
            Path(result["outputs"]["chinese_srt"]).write_text(
                "1\n00:00:00,500 --> 00:00:00,500\n中文\n",
                encoding="utf-8",
            )
            calls.clear()
            configs.clear()

            second = self._runner(root, calls, configs)
            with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                second.run()

            self.assertEqual([], calls)

    def test_resume_rejects_missing_declared_subtitle_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            first = self._runner(root, calls, configs)
            first.run()
            result_path = first._translation_result_path()
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["outputs"]["subtitle_review_md"] = str(
                root / "out" / "missing.subtitle-review.md"
            )
            result_path.write_text(json.dumps(result), encoding="utf-8")
            calls.clear()

            second = self._runner(root, calls, configs)
            with self.assertRaisesRegex(FileNotFoundError, "subtitle_review_md"):
                second.run()

            self.assertEqual([], calls)

    def test_picture_failure_keeps_stage_outputs_but_publishes_no_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(root, calls, configs)

            def fail_picture(*args, **kwargs):
                calls.append("picture")
                raise RuntimeError("synthetic picture failure")

            runner.picture_runner = fail_picture
            with self.assertRaisesRegex(RuntimeError, "synthetic picture failure"):
                runner.run()

            self.assertTrue(runner._translation_result_path().is_file())
            self.assertFalse(runner.paths.final_output.exists())
            self.assertFalse(runner.paths.result.exists())
            manifest = json.loads(runner.paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")

    def test_resume_after_picture_gate_failure_reuses_completed_translation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            failed = self._runner(root, calls, configs)

            def fail_picture(*args, **kwargs):
                calls.append("picture")
                raise RuntimeError("synthetic route gate failure")

            failed.picture_runner = fail_picture
            with self.assertRaisesRegex(RuntimeError, "synthetic route gate failure"):
                failed.run()

            self.assertTrue(failed._translation_result_path().is_file())
            self.assertFalse(failed.paths.picture_work_dir.exists())
            self.assertFalse(failed.paths.final_mux_work_dir.exists())
            self.assertFalse(failed.paths.processed_video.exists())
            self.assertFalse(failed.paths.final_output.exists())
            failed_manifest = json.loads(
                failed.paths.manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(failed_manifest["status"], "failed")

            calls.clear()
            configs.clear()
            resumed = self._runner(root, calls, configs)
            resumed.api_key = ""
            result = resumed.run()

            self.assertEqual(calls, ["picture", "final_mux"])
            self.assertTrue(result["stages"]["translation"]["reused"])
            self.assertEqual(result["status"], "technical_checks_passed")
            current_manifest = json.loads(
                resumed.paths.manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(current_manifest["status"], "technical_checks_passed")

    def test_translation_failure_after_fusion_allows_picture_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            runner = self._runner(root, calls, configs)

            class FailingTranslationRunner:
                def __init__(self, request, api_key, **kwargs) -> None:
                    self.emitter = kwargs["emitter"]
                    self.on_asr_ready = kwargs["on_asr_ready"]

                def run(self):
                    calls.append("translation")
                    self.emitter.emit("job_started")
                    self.on_asr_ready()
                    raise RuntimeError("synthetic translation failure after fusion")

            runner.translation_runner_factory = FailingTranslationRunner
            with self.assertRaisesRegex(
                RuntimeError, "synthetic translation failure after fusion"
            ):
                runner.run()

            self.assertEqual(["translation", "picture"], calls)
            self.assertTrue(runner.paths.processed_video.is_file())
            self.assertFalse(runner.paths.final_output.exists())
            self.assertFalse(runner.paths.result.exists())

    def test_request_requires_explicit_human_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "movie.mkv"
            source.write_bytes(b"source")
            payload = {
                "schema_version": 1,
                "workflow_mode": "translation_picture_final",
                "job_id": "job-gate",
                "input_video": str(source),
                "output_dir": str(root / "out"),
                "profile_id": "normal",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": "deepseek-v4-pro",
                },
                "consent": {
                    "current_run": True,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
                "gates": {"picture_and_mux_30m_human_accepted": False},
            }
            with self.assertRaisesRegex(ValueError, "30 分钟"):
                FullWorkflowRequest.from_mapping(payload)

    def test_desktop_stdout_remains_one_monotonic_jsonl_event_stream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls: list[str] = []
            configs: list[object] = []
            stream = StringIO()
            runner = self._runner(root, calls, configs)
            runner.events = EventEmitter(
                runner.request.translation.job_id,
                runner.paths.workflow_root / "desktop-events.jsonl",
                stdout=stream,
            )
            original_picture = runner.picture_runner

            def noisy_picture(*args, **kwargs):
                if kwargs.get("mirror_output", True):
                    print("raw Jasna progress that must not reach desktop stdout")
                return original_picture(*args, **kwargs)

            runner.picture_runner = noisy_picture
            runner.run()

            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertTrue(events)
            self.assertEqual(
                [item["seq"] for item in events],
                list(range(1, len(events) + 1)),
            )
            self.assertEqual(events[0]["event"], "job_started")
            self.assertEqual(events[-1]["event"], "job_completed")
            self.assertNotIn("raw Jasna progress", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
