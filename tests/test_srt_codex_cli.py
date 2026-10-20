from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from translation_flow.srt import parse_srt
from translation_flow.translation.cli import main
from translation_flow.translation.codex_provider import (
    CodexLoginRequired, CodexQuotaExceeded, TranslationCancelled,
)


FIXTURE = Path(__file__).parent / "fixtures" / "api-acceptance.ja.srt"


class FakeCodex:
    provider_id = "codex"
    cache_identity = "codex:srt-cli-test:chatgpt"

    def __init__(self, *, cancelled=lambda: False, **kwargs):
        self.cancelled = cancelled
        self.calls = []

    def check_cancelled(self):
        if self.cancelled():
            raise TranslationCancelled("成功批次已保留，可用相同参数继续。")

    def chat(self, messages, *, model, temperature, json_output=False):
        self.check_cancelled()
        self.calls.append(messages)
        if not json_output:
            return "会议准备与资料确认。"
        content = messages[-1]["content"]
        payload = json.JSONDecoder().raw_decode(content[content.rfind('{"items":'):])[0]
        return json.dumps({"items": [
            {"id": item["id"], "text": f"中文译文{item['id']}",
             "note": "称呼需人工核对。" if item["id"] == 1 else ""}
            for item in payload["items"]
        ]}, ensure_ascii=False)


class SrtCodexCliTests(unittest.TestCase):
    def args(self, root, stem="zh"):
        return [
            "--input", str(FIXTURE), "--output", str(root / f"{stem}.srt"),
            "--report", str(root / f"{stem}.json"), "--cache-dir", str(root / "cache"),
            "--provider", "codex", "--consent-subtitle-text-to-provider", "codex",
            "--batch-size", "4",
        ]

    def test_codex_needs_no_key_and_does_not_inherit_api_model(self):
        for timeout_args, expected_timeout in (([], 300), (["--timeout", "120"], 120)):
            with self.subTest(timeout=expected_timeout), tempfile.TemporaryDirectory() as temp, patch.dict(
                os.environ, {"SUBTITLE_MODEL": "unrelated-api-model"}, clear=True
            ), patch("translation_flow.translation.cli.CodexProvider") as codex, patch(
                "translation_flow.translation.cli.create_api_provider"
            ) as api, patch("translation_flow.translation.cli.run_translation", return_value={"warnings": []}) as run, patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                previous = signal.getsignal(signal.SIGINT)
                root = Path(temp)
                self.assertEqual(0, main(self.args(root) + timeout_args))
                api.assert_not_called()
                self.assertEqual("cli-default", codex.call_args.kwargs["model"])
                self.assertEqual(expected_timeout, codex.call_args.kwargs["timeout"])
                self.assertEqual(root / "cache" / "codex-calls", codex.call_args.kwargs["audit_dir"].parent)
                self.assertNotIn("api_key", codex.call_args.kwargs)
                self.assertIs(codex.return_value, run.call_args.args[5])
                self.assertEqual("cli-default", run.call_args.args[6])
                self.assertIs(previous, signal.getsignal(signal.SIGINT))

    def test_bad_consent_and_api_only_options_fail_before_provider_construction(self):
        base = ["--input", "in.srt", "--output", "out.srt", "--provider", "codex"]
        consent = ["--consent-subtitle-text-to-provider", "codex"]
        cases = [
            [], ["--consent-subtitle-text-to-provider", "openai"],
            ["--consent-subtitle-text-to-deepseek"],
            consent + ["--consent-subtitle-text-to-deepseek"],
            consent + ["--model", "an-api-model"],
            consent + ["--endpoint-profile", "openai"],
            consent + ["--max-output-tokens", "8192"],
            consent + ["--timeout", "0"],
        ]
        for extra in cases:
            with self.subTest(extra=extra), patch("translation_flow.translation.cli.CodexProvider") as codex, patch(
                "translation_flow.translation.cli.create_api_provider"
            ) as api, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    main(base + extra)
                self.assertEqual(2, error.exception.code)
                codex.assert_not_called()
                api.assert_not_called()

    def test_full_srt_pipeline_notes_and_cache_replay_without_media_or_api(self):
        provider = FakeCodex()
        original = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as temp, patch(
            "translation_flow.translation.cli.CodexProvider", return_value=provider
        ), patch("translation_flow.translation.cli.create_api_provider") as api, patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            root = Path(temp)
            self.assertEqual(0, main(self.args(root)))
            self.assertEqual(5, len(provider.calls))  # context, two initial, two polish
            source = parse_srt(FIXTURE)
            target = parse_srt(root / "zh.srt")
            self.assertEqual([(c.index, c.start_ms, c.end_ms) for c in source],
                             [(c.index, c.start_ms, c.end_ms) for c in target])
            self.assertTrue(all(c.text == f"中文译文{c.index}" for c in target))
            report = json.loads((root / "zh.json").read_text(encoding="utf-8"))
            self.assertEqual(("codex", "cli-default", "passed"),
                             (report["provider"], report["model"], report["status"]))
            self.assertEqual({"translate", "polish"}, {item["phase"] for item in report["uncertainties"]})
            self.assertEqual(0, main(self.args(root, "replay")))
            self.assertEqual(5, len(provider.calls))
            self.assertEqual((root / "zh.srt").read_bytes(), (root / "replay.srt").read_bytes())
            self.assertEqual(original, FIXTURE.read_bytes())
            api.assert_not_called()

    def test_existing_results_are_not_overwritten_even_with_force(self):
        for filename in ("zh.srt", "zh.json"):
            provider = FakeCodex()
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp, patch(
                "translation_flow.translation.cli.CodexProvider", return_value=provider
            ), patch("sys.stdout", new_callable=io.StringIO):
                root = Path(temp)
                existing = root / filename
                existing.write_bytes(b"keep-existing-result")
                with self.assertRaises(FileExistsError):
                    main(self.args(root) + ["--force"])
                self.assertEqual(b"keep-existing-result", existing.read_bytes())
                self.assertEqual([], provider.calls)

    def test_login_and_quota_stop_without_retries_or_api_fallback(self):
        for failure in (CodexLoginRequired, CodexQuotaExceeded):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp, patch(
                "translation_flow.translation.cli.CodexProvider"
            ) as codex, patch("translation_flow.translation.cli.create_api_provider") as api, patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                provider = FakeCodex()
                codex.return_value = provider
                root = Path(temp)
                previous = signal.getsignal(signal.SIGINT)
                with patch.object(provider, "chat", side_effect=failure("stop")) as chat:
                    with self.assertRaises(failure):
                        main(self.args(root))
                    self.assertEqual(1, chat.call_count)
                self.assertFalse((root / "zh.srt").exists())
                self.assertFalse((root / "zh.json").exists())
                self.assertIs(previous, signal.getsignal(signal.SIGINT))
                api.assert_not_called()

    def test_ctrl_c_preserves_completed_batch_and_resume_only_calls_missing_batch(self):
        class StopSecond(FakeCodex):
            def chat(self, *args, **kwargs):
                if self.calls:
                    signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                return super().chat(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            root = Path(temp)
            args = self.args(root) + ["--no-global-context", "--skip-polish", "--concurrency", "1"]
            previous = signal.getsignal(signal.SIGINT)
            with patch("translation_flow.translation.cli.CodexProvider", side_effect=StopSecond):
                self.assertEqual(2, main(args))
            self.assertIn("[cancelled]", stdout.getvalue())
            self.assertFalse((root / "zh.srt").exists())
            self.assertEqual(1, len(list((root / "cache").glob("v1/*/batches/translate/*.json"))))
            provider = FakeCodex()
            with patch("translation_flow.translation.cli.CodexProvider", return_value=provider):
                self.assertEqual(0, main(args))
            self.assertEqual(1, len(provider.calls))
            report = json.loads((root / "zh.json").read_text(encoding="utf-8"))
            self.assertEqual(1, report["uncertainties"][0]["id"])
            self.assertIs(previous, signal.getsignal(signal.SIGINT))

    def test_ctrl_c_reaches_both_running_lanes(self):
        rendezvous = threading.Barrier(2)
        observed = []

        class TwoLanes(FakeCodex):
            def chat(self, *args, **kwargs):
                rendezvous.wait(timeout=5)
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                observed.append(self.cancelled())
                return super().chat(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp, patch(
            "translation_flow.translation.cli.CodexProvider", side_effect=TwoLanes
        ), patch("sys.stdout", new_callable=io.StringIO):
            root = Path(temp)
            self.assertEqual(2, main(self.args(root) + ["--no-global-context"]))
            self.assertEqual([True, True], observed)
            self.assertFalse((root / "zh.srt").exists())


if __name__ == "__main__":
    unittest.main()
