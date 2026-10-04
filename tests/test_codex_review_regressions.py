from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from full_workflow.contracts import FullWorkflowRequest
from translation_flow.srt import SubtitleCue, parse_srt, write_srt
from translation_flow.translation.codex_provider import CodexProvider, TranslationCancelled
from translation_flow.translation.pipeline import _chat_with_retries, run_translation
from translation_flow.translation.provider import FatalProviderError


class CancelAfterRepairProvider:
    provider_id = "codex"
    cache_identity = "codex-cancel-repair-regression"

    def __init__(self):
        self.cancelled = False
        self.stop_after_repair = True
        self.calls: list[str] = []

    def check_cancelled(self):
        if self.cancelled:
            raise TranslationCancelled("test cancellation after successful repair")

    def chat(self, messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("定点修复"):
            phase = "repair"
        elif system.startswith("对照日文润色"):
            phase = "polish"
        else:
            phase = "initial"
        self.calls.append(phase)
        if phase == "repair" and self.stop_after_repair:
            self.cancelled = True
        return json.dumps({
            "items": [{"id": 1, "text": "はい" if phase == "polish" else "好的", "note": ""}],
            "_codex": {},
        }, ensure_ascii=False)


class CodexReviewRegressionTests(unittest.TestCase):
    def test_resume_after_successful_kana_repair_does_not_call_codex_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = write_srt(root / "ja.srt", [SubtitleCue(1, 0, 1000, "はい")])
            output = root / "zh.srt"
            provider = CancelAfterRepairProvider()
            args = (source, output, root / "report.json", root / "cache", "normal", provider, "cli-default")
            with self.assertRaises(TranslationCancelled):
                run_translation(*args, use_global_context=False)
            self.assertEqual(["initial", "polish", "repair"], provider.calls)
            self.assertFalse(output.exists())
            self.assertEqual(3, len(list((root / "cache").glob("**/batches/*/*.json"))))

            provider.cancelled = False
            provider.stop_after_repair = False
            report = run_translation(*args, use_global_context=False)
            self.assertEqual(["initial", "polish", "repair"], provider.calls)
            self.assertEqual("好的", parse_srt(output)[0].text)
            self.assertEqual("passed", report["status"])

    def test_legacy_deepseek_full_workflow_accepts_null_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"contract-only source placeholder")
            payload = {
                "schema_version": 1, "workflow_mode": "translation_picture_final",
                "job_id": "legacy-deepseek", "input_video": str(source),
                "output_dir": str(root), "profile_id": "normal",
                "provider": {"id": "deepseek", "host": "api.deepseek.com", "model": "deepseek-v4-pro"},
                "consent": {"current_run": True, "subtitle_text_only": True, "audio_video_local": True},
                "gates": {"picture_and_mux_30m_human_accepted": True}, "delivery": None,
            }
            request = FullWorkflowRequest.from_mapping(payload)
            self.assertEqual("deepseek", request.translation.provider.id)
            self.assertFalse(request.delivery.additional_mp4)
            self.assertFalse(request.translation.additional_mp4)
            for invalid in ([], "", {"additional_mp4": "true"}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    FullWorkflowRequest.from_mapping({**payload, "delivery": invalid})

    def test_completed_codex_call_is_not_retried_when_audit_write_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            audit = Path(temp) / "audit"
            calls = []

            class CompletedProcess:
                returncode = 0

                def __init__(self, args, **kwargs):
                    calls.append(args)
                    output = Path(args[args.index("--output-last-message") + 1])
                    output.write_text("已返回的合成语境", encoding="utf-8")

                def communicate(self, **kwargs):
                    return json.dumps({"type": "turn.completed"}) + "\n", ""

            provider = CodexProvider(
                audit_dir=audit, executable="fake-official-codex",
                status_probe=lambda **kwargs: {"status": "available", "cli_version": "test"},
                process_factory=CompletedProcess,
            )
            original_write = Path.write_text

            def fail_audit_write(path, *args, **kwargs):
                if path.parent == audit:
                    raise OSError("simulated disk full")
                return original_write(path, *args, **kwargs)

            with mock.patch.object(Path, "write_text", fail_audit_write):
                with self.assertRaises(FatalProviderError):
                    _chat_with_retries(
                        provider, [{"role": "system", "content": "合成文本测试"}],
                        model="cli-default", temperature=0, retries=2,
                    )
            self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
