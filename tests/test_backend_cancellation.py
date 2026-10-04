from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from translation_flow.cancellation import CancellableProvider, check_cancelled
from translation_flow.contracts import JobRequest
from translation_flow.translation.api_providers import API_PROVIDERS
from translation_flow.translation.codex_provider import TranslationCancelled
from translation_flow.translation.pipeline import run_translation
from translation_flow.translation.pipeline import _chat_with_retries
from translation_flow.translation.provider import RetryableProviderError
import test_video_api_translation as api_fixtures
import test_codex_integration as codex_fixtures


class BackendCancellationTests(unittest.TestCase):
    def test_stop_marker_reaches_asr_before_it_returns_for_codex_and_api(self):
        class StoppingAsr:
            def __init__(self, tools):
                pass

            def run(self, source, work, emit, *, cancelled):
                emit.emit("stage_started", stage="asr_a")
                (work.parent / "cancel.requested").write_text("stop", encoding="utf-8")
                check_cancelled(cancelled)
                raise AssertionError("ASR did not receive the runner's stop marker")

        for name in ("codex", "qwen"):
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                if name == "codex":
                    helper = codex_fixtures.CodexIntegrationTests()
                    helper.setUp()
                    runner = helper.runner(helper.request(root))
                    fake_tools = codex_fixtures.FakeTools
                else:
                    harness = api_fixtures.Harness(name)
                    runner = harness.runner(JobRequest.from_mapping(api_fixtures.payload(root, name)))
                    fake_tools = api_fixtures.FakeTools
                runner.asr_factory = StoppingAsr
                with patch("translation_flow.runner.ToolPaths", fake_tools):
                    with self.assertRaises(TranslationCancelled):
                        runner.run()
                self.assertFalse((runner.job_root / "asr/asr.report.json").exists())
                if name == "codex":
                    self.assertEqual([], codex_fixtures.FakeCodex.calls)
                else:
                    self.assertEqual([], harness.requests)

    def test_stop_interrupts_long_retry_after_without_a_second_http_call(self):
        raw = Mock()
        error = RetryableProviderError("rate limited")
        error.retry_after_seconds = 300
        raw.chat.side_effect = error
        stopped = False

        def stop(_seconds):
            nonlocal stopped
            stopped = True

        provider = CancellableProvider(raw, lambda: stopped)
        with patch("translation_flow.cancellation.time.sleep", side_effect=stop) as sleep:
            with self.assertRaises(TranslationCancelled):
                _chat_with_retries(provider, [], model="fixture", temperature=0, retries=4)
            sleep.assert_called_once_with(1.0)
        raw.chat.assert_called_once()

    def test_each_api_stops_after_current_response_and_resumes_cached_initial_batch(self):
        for name in API_PROVIDERS:
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as temp, patch(
                "translation_flow.runner.ToolPaths", api_fixtures.FakeTools
            ):
                root = Path(temp)
                request = JobRequest.from_mapping(api_fixtures.payload(root, name))
                harness = api_fixtures.Harness(name)
                marker = request.output_dir / ".translation-flow" / request.job_id / "cancel.requested"
                opener = harness.opener
                stop_once = True

                def stop_after_initial(*args, **kwargs):
                    nonlocal stop_once
                    response = opener(*args, **kwargs)
                    if stop_once and harness.successes[-1][0] == "initial":
                        marker.write_text("stop", encoding="utf-8")
                        stop_once = False
                    return response

                def translate(*args, **kwargs):
                    kwargs["concurrency"] = 1
                    return run_translation(*args, **kwargs, batch_size=1, retries=1)

                harness.opener, harness.translate = stop_after_initial, translate
                with self.assertRaises(TranslationCancelled):
                    harness.runner(request).run()
                self.assertEqual(0, harness.package_calls)
                self.assertEqual(1, harness.successes.count(("initial", 1)))
                self.assertFalse(any(phase == "polish" for phase, _ in harness.successes))
                result = harness.runner(request).run()
                self.assertEqual(1, harness.successes.count(("initial", 1)))
                self.assertEqual("passed", result["technical_validation"]["status"])

    def test_stop_after_asr_prevents_provider_construction_for_api(self):
        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", api_fixtures.FakeTools):
            request = JobRequest.from_mapping(api_fixtures.payload(Path(temp)))
            harness = api_fixtures.Harness("qwen")
            stopped = False
            asr = harness.run

            def finish_asr(*args, **kwargs):
                nonlocal stopped
                result = asr(*args, **kwargs)
                stopped = True
                return result

            harness.run = finish_asr
            with self.assertRaises(TranslationCancelled):
                harness.runner(request, cancelled=lambda: stopped).run()
            self.assertEqual([], harness.requests)
            self.assertEqual(0, harness.package_calls)

    def test_deepseek_original_can_append_mp4_after_primary_result_is_saved(self):
        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", api_fixtures.FakeTools):
            request = replace(JobRequest.from_mapping(api_fixtures.payload(Path(temp), "deepseek")), additional_mp4=True)
            harness = api_fixtures.Harness("deepseek")

            def delivery(runner, result):
                saved = json.loads((runner.job_root / "job.result.json").read_text(encoding="utf-8"))
                self.assertTrue(Path(saved["outputs"]["final_mkv"]).is_file())
                result.setdefault("stages", {})["mp4_delivery"] = {"status": "technical_checks_passed"}

            with patch("translation_flow.codex_runner._append_mp4", side_effect=delivery) as append:
                result = harness.runner(request).run()
            append.assert_called_once()
            self.assertEqual("technical_checks_passed", result["stages"]["mp4_delivery"]["status"])
