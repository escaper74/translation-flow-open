from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL, JobRequest
from translation_flow.events import EventEmitter
from translation_flow.runner import TranslationFlowRunner
from translation_flow.srt import SubtitleCue, subtitle_digest, write_srt
from translation_flow.translation import DeepSeekProvider


class _Provider:
    def __init__(self, model: str | None = None) -> None:
        self.checked = False
        self.model = model

    def check_connection(self) -> None:
        self.checked = True


class _Asr:
    def __init__(self, tools: object) -> None:
        self.tools = tools

    def run(self, source: Path, job_root: Path, emit: object = None, *, cancelled=None):
        self.assert_secret_not_in_environment()
        output = job_root / "fusion.ja.srt"
        write_srt(output, [SubtitleCue(1, 0, 1000, "はい")])
        return output, {"status": "complete", "timeline_preserved_from_a": True}

    @staticmethod
    def assert_secret_not_in_environment() -> None:
        if "SUBTITLE_API_KEY" in os.environ:
            raise AssertionError("ASR must not inherit the DeepSeek credential")


class _ZeroDurationAsr(_Asr):
    def run(self, source: Path, job_root: Path, emit: object = None, *, cancelled=None):
        output = job_root / "fusion.ja.srt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "1\n00:00:00,500 --> 00:00:00,500\n声\n",
            encoding="utf-8",
        )
        return output, {"status": "complete", "timeline_preserved_from_a": True}


class _Packager:
    def __init__(self, ffmpeg: Path, ffprobe: Path) -> None:
        pass

    def package(
        self,
        source: Path,
        subtitle: Path,
        output: Path,
        report: Path,
        emit: object = None,
    ) -> Path:
        output.write_bytes(b"mkv")
        report.write_text('{"status":"complete"}', encoding="utf-8")
        return output


class _Tools:
    ffmpeg = Path("ffmpeg")
    ffprobe = Path("ffprobe")

    @classmethod
    def from_root(cls, *args, **kwargs):
        return cls()

    @staticmethod
    def required_paths():
        return ()


class RunnerTests(unittest.TestCase):
    def _request(self, directory: Path) -> JobRequest:
        source = directory / "movie.mkv"
        source.write_bytes(b"source")
        return JobRequest.from_mapping(
            {
                "schema_version": 1,
                "job_id": "12345678-aaaa-bbbb-cccc-123456789abc",
                "input_video": str(source),
                "output_dir": str(directory / "out"),
                "profile_id": "adult_direct",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": DEFAULT_DEEPSEEK_MODEL,
                },
                "consent": {
                    "current_run": True,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
            }
        )

    def test_offline_orchestration_emits_strict_success_contract(self) -> None:
        import translation_flow.runner as runner_module

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = self._request(root)
            output = io.StringIO()
            event_path = request.output_dir / ".translation-flow" / request.job_id / "events.jsonl"
            emitter = EventEmitter(request.job_id, event_path, output)
            provider = _Provider()
            asr_handoff: list[str] = []

            def provider_factory(*args, **kwargs):
                self.assertEqual(["ready"], asr_handoff)
                provider.model = kwargs.get("model")
                return provider

            def translator(input_srt, output_srt, report_path, *args, **kwargs):
                self.assertNotIn("SUBTITLE_API_KEY", os.environ)
                self.assertEqual(2, kwargs.get("concurrency"))
                write_srt(output_srt, [SubtitleCue(1, 0, 1000, "是")])
                Path(report_path).write_text(
                    json.dumps({"status": "passed", "warnings": []}), encoding="utf-8"
                )
                return {"status": "passed", "warnings": []}

            original_tools = runner_module.ToolPaths
            runner_module.ToolPaths = _Tools
            os.environ["SUBTITLE_API_KEY"] = "sentinel-inherited-secret"
            try:
                result = TranslationFlowRunner(
                    request,
                    "sentinel-secret",
                    provider_factory=provider_factory,
                    asr_factory=_Asr,
                    packager_factory=_Packager,
                    translator=translator,
                    emitter=emitter,
                    on_asr_ready=lambda: asr_handoff.append("ready"),
                ).run()
            finally:
                os.environ.pop("SUBTITLE_API_KEY", None)
                runner_module.ToolPaths = original_tools

            self.assertTrue(provider.checked)
            self.assertEqual(DEFAULT_DEEPSEEK_MODEL, provider.model)
            self.assertEqual("passed", result["technical_validation"]["status"])
            self.assertEqual("pending", result["human_review_status"])
            self.assertTrue(Path(result["outputs"]["chinese_srt"]).is_file())
            review_path = Path(result["outputs"]["subtitle_review_md"])
            self.assertTrue(review_path.is_file())
            review_text = review_path.read_text(encoding="utf-8")
            self.assertIn("| 时间 | 日文 | 最终中文 |", review_text)
            self.assertIn("| 00:00:00.000–00:00:01.000 | はい | 是 |", review_text)
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())
            persisted = event_path.read_text(encoding="utf-8")
            self.assertNotIn("sentinel-secret", persisted)
            self.assertEqual("job_completed", json.loads(persisted.splitlines()[-1])["event"])

    def test_invalid_asr_handoff_is_rejected_before_provider_call(self) -> None:
        import translation_flow.runner as runner_module

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = self._request(root)
            provider_calls = 0
            handoff_calls = 0

            def on_asr_ready() -> None:
                nonlocal handoff_calls
                handoff_calls += 1

            def provider_factory(*args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                return _Provider()

            original_tools = runner_module.ToolPaths
            runner_module.ToolPaths = _Tools
            try:
                with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                    TranslationFlowRunner(
                        request,
                        "secret",
                        provider_factory=provider_factory,
                        asr_factory=_ZeroDurationAsr,
                        packager_factory=_Packager,
                        on_asr_ready=on_asr_ready,
                    ).run()
            finally:
                runner_module.ToolPaths = original_tools

            self.assertEqual(0, provider_calls)
            self.assertEqual(0, handoff_calls)

    def test_refuses_existing_final_result_before_provider_call(self) -> None:
        import translation_flow.runner as runner_module

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = self._request(root)
            request.output_dir.mkdir(parents=True)
            existing = request.output_dir / "movie.adult-direct.12345678-aaa.zh-CN.srt"
            existing.write_text("keep", encoding="utf-8")
            provider_calls = 0

            def provider_factory(*args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                return _Provider()

            original_tools = runner_module.ToolPaths
            runner_module.ToolPaths = _Tools
            try:
                with self.assertRaises(FileExistsError):
                    TranslationFlowRunner(
                        request,
                        "secret",
                        provider_factory=provider_factory,
                        asr_factory=_Asr,
                        packager_factory=_Packager,
                    ).run()
            finally:
                runner_module.ToolPaths = original_tools
            self.assertEqual(0, provider_calls)
            self.assertEqual("keep", existing.read_text(encoding="utf-8"))

    def test_packaging_retry_reuses_valid_translation_checkpoint_without_provider(self) -> None:
        import translation_flow.runner as runner_module

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = self._request(root)
            job_root = request.output_dir / ".translation-flow" / request.job_id
            job_root.mkdir(parents=True)
            run_label = request.job_id[:12]
            chinese_srt = request.output_dir / (
                f"movie.adult-direct.{run_label}.zh-CN.srt"
            )
            fused_cues = [SubtitleCue(1, 0, 1000, "はい")]
            write_srt(chinese_srt, [SubtitleCue(1, 0, 1000, "是")])
            (job_root / "translation.report.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "profile": "adult_direct",
                        "provider": DeepSeekProvider.provider_id,
                        "model": DEFAULT_DEEPSEEK_MODEL,
                        "source_sha256": subtitle_digest(fused_cues),
                        "output": str(chinese_srt),
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            provider_calls = 0
            translator_calls = 0

            def provider_factory(*args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                return _Provider()

            def translator(*args, **kwargs):
                nonlocal translator_calls
                translator_calls += 1
                raise AssertionError("translator must not run")

            original_tools = runner_module.ToolPaths
            runner_module.ToolPaths = _Tools
            try:
                result = TranslationFlowRunner(
                    request,
                    "unused-on-resume",
                    provider_factory=provider_factory,
                    asr_factory=_Asr,
                    packager_factory=_Packager,
                    translator=translator,
                ).run()
            finally:
                runner_module.ToolPaths = original_tools

            self.assertEqual(0, provider_calls)
            self.assertEqual(0, translator_calls)
            self.assertTrue(Path(result["outputs"]["final_mkv"]).is_file())

    def test_translation_checkpoint_rejects_zero_duration_chinese_output(self) -> None:
        import translation_flow.runner as runner_module

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = self._request(root)
            job_root = request.output_dir / ".translation-flow" / request.job_id
            job_root.mkdir(parents=True)
            run_label = request.job_id[:12]
            chinese_srt = request.output_dir / (
                f"movie.adult-direct.{run_label}.zh-CN.srt"
            )
            chinese_srt.write_text(
                "1\n00:00:00,500 --> 00:00:00,500\n中文\n",
                encoding="utf-8",
            )
            fused_cues = [SubtitleCue(1, 0, 1000, "はい")]
            (job_root / "translation.report.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "profile": "adult_direct",
                        "provider": DeepSeekProvider.provider_id,
                        "model": DEFAULT_DEEPSEEK_MODEL,
                        "source_sha256": subtitle_digest(fused_cues),
                        "output": str(chinese_srt),
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )
            provider_calls = 0

            def provider_factory(*args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                return _Provider()

            original_tools = runner_module.ToolPaths
            runner_module.ToolPaths = _Tools
            try:
                with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                    TranslationFlowRunner(
                        request,
                        "unused-on-resume",
                        provider_factory=provider_factory,
                        asr_factory=_Asr,
                        packager_factory=_Packager,
                    ).run()
            finally:
                runner_module.ToolPaths = original_tools

            self.assertEqual(0, provider_calls)
            self.assertFalse(
                (request.output_dir / f"movie.adult-direct.{run_label}.zh-CN.mkv").exists()
            )


if __name__ == "__main__":
    unittest.main()
