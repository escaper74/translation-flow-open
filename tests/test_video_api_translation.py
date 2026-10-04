from __future__ import annotations

from collections import Counter
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from full_workflow.contracts import FullWorkflowRequest
from full_workflow.runner import FullWorkflowRunner
from translation_flow.contracts import ApiProviderRequest, JobRequest, ProviderRequest
from translation_flow.events import EventEmitter
from translation_flow.runner import TranslationFlowRunner
from translation_flow.srt import SubtitleCue, parse_srt, write_srt
from translation_flow.translation.api_providers import API_PROVIDERS, create_api_provider, resolve_endpoint
from translation_flow.translation.pipeline import run_translation
from translation_flow.translation.provider import FatalProviderError
from tests.test_api_providers import Response, envelope
from tests import test_full_workflow as full_fixture


def payload(root, name="qwen", **provider_changes):
    source = root / "movie.mkv"
    if not source.exists():
        source.write_bytes(b"read-only source fixture, not media")
    provider = {"id": name, "model": "explicit-fixture-model"}
    if name == "deepseek":
        provider.update(host="api.deepseek.com", model="deepseek-v4-pro")
    if name == "codex":
        provider.update(host="chatgpt.com", model="cli-default")
    provider.update(provider_changes)
    return {
        "schema_version": 1, "job_id": "job-video-api", "workflow_mode": "translation",
        "input_video": str(source), "output_dir": str(root / "out"), "profile_id": "normal",
        "provider": provider,
        "consent": {"current_run": True, "subtitle_text_only": True, "audio_video_local": True,
                    "provider": name},
    }


class FakeTools:
    ffmpeg = Path("unused-ffmpeg")
    ffprobe = Path("unused-ffprobe")

    @classmethod
    def from_root(cls, *args, **kwargs):
        return cls()

    def required_paths(self):
        return ()


class Harness:
    def __init__(self, name):
        self.name = name
        self.asr_calls = 0
        self.package_calls = 0
        self.requests = []
        self.successes = []
        self.fail_polish = False
        self.fail_package = False

    def asr(self, tools):
        return self

    def run(self, source, work, emit=None, *, cancelled=None):
        self.asr_calls += 1
        if "SUBTITLE_API_KEY" in os.environ:
            raise AssertionError("ASR inherited API Key")
        fusion = write_srt(work / "fusion" / "fusion.ja.srt", [
            SubtitleCue(i, i * 1000, i * 1000 + 900, "おはよう") for i in range(1, 5)
        ])
        write_srt(work / "A" / "A.ja.srt", parse_srt(fusion))
        write_srt(work / "C" / "C.ja.srt", parse_srt(fusion))
        stat = source.stat()
        report = {"status": "complete", "source": str(source), "source_size_bytes": stat.st_size,
                  "source_mtime_ns": stat.st_mtime_ns, "fusion_srt": str(fusion), "fusion_cues": 4,
                  "a_cues": 4, "c_cues": 4, "timeline_preserved_from_a": True}
        (work / "asr.report.json").write_text(json.dumps(report), encoding="utf-8")
        return fusion, report

    def packager(self, *args, **kwargs):
        return self

    def package(self, source, subtitle, output, report_path, emit=None):
        self.package_calls += 1
        if self.fail_package:
            raise RuntimeError("synthetic packaging failure")
        output.write_bytes(b"synthetic package, no FFmpeg")
        stat = output.stat()
        report_path.write_text(json.dumps({
            "status": "passed", "checks": {"synthetic": True}, "source": str(source),
            "chinese_srt": str(subtitle), "output": str(output), "input_subtitle_cues": 4,
            "output_file": {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        }), encoding="utf-8")
        return output

    def opener(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append((request.full_url, body))
        messages = body.get("input", body.get("messages"))
        system = body.get("system", messages[0]["content"])
        user = messages[-1]["content"]
        if system.startswith(("为日语", "合并分段")):
            self.successes.append(("context", 0))
            return Response(envelope(self.name, "问候场景"))
        items = json.loads(user[user.rfind('{"items"'):])["items"]
        phase = "probe" if system.startswith("Translate the single") else (
            "polish" if system.startswith("对照日文润色") else "initial")
        if self.fail_polish and phase == "polish" and items[0]["id"] == 2:
            raise FatalProviderError("synthetic quota failure")
        self.successes.extend((phase, item["id"]) for item in items)
        return Response(envelope(self.name, json.dumps({"items": [
            {"id": item["id"], "text": "早上好"} for item in items
        ]})))

    def provider(self, name, key, **kwargs):
        return create_api_provider(name, key, opener=self.opener, **kwargs)

    def legacy_provider(self, key, *, model, api_base):
        if api_base != "https://api.deepseek.com":
            raise AssertionError("legacy base changed")
        return create_api_provider("deepseek", key, model=model, opener=self.opener)

    def translate(self, *args, **kwargs):
        # Exercise real context, fixed two lanes, phase barrier, atomic caches.
        return run_translation(*args, **kwargs, batch_size=1, retries=1)

    def runner(self, request, key="fixture-key", **kwargs):
        return TranslationFlowRunner(
            request, key, api_provider_factory=self.provider, provider_factory=self.legacy_provider,
            asr_factory=self.asr, packager_factory=self.packager, translator=self.translate,
            emitter=kwargs.pop("emitter", None) or EventEmitter(
                request.job_id, request.output_dir / ".translation-flow" / request.job_id / "events.jsonl",
                stdout=io.StringIO()), **kwargs,
        )


class VideoApiContractTests(unittest.TestCase):
    def test_old_provider_serialization_and_explicit_new_defaults(self):
        sample = parse_srt(Path(__file__).parent / "fixtures" / "api-acceptance.ja.srt")
        self.assertEqual((8, 0, 45000), (len(sample), sample[0].start_ms, sample[-1].end_ms))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("deepseek", "codex"):
                request = JobRequest.from_mapping(payload(root, name))
                self.assertEqual({"id", "host", "model"}, set(vars(request.provider)))
                self.assertEqual(ProviderRequest, type(request.provider))
            implicit = JobRequest.from_mapping(payload(root)).provider
            explicit = JobRequest.from_mapping(payload(root, endpoint_profile="qwen-cn", max_output_tokens=8192)).provider
            self.assertEqual(implicit, explicit)
            self.assertEqual("dashscope.aliyuncs.com", implicit.host)

    def test_invalid_config_and_consent_fail_before_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for changes in ({"model": ""}, {"model": 42}, {"endpoint_profile": "openai"},
                            {"endpoint_profile": ""}, {"host": "unselected.example"},
                            {"max_output_tokens": True}, {"max_output_tokens": 0},
                            {"max_output_tokens": "8192"}, {"api_key": "not-allowed"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    JobRequest.from_mapping(payload(root, **changes))
            for consent in ({}, {"provider": "openai"}, {"current_run": False}):
                data = payload(root)
                data["consent"] = data["consent"] | consent if consent else {}
                with self.assertRaises(ValueError):
                    JobRequest.from_mapping(data)
            with self.assertRaisesRegex(ValueError, "输出上限"):
                JobRequest.from_mapping(payload(root, "deepseek", max_output_tokens=8192))
            self.assertFalse((root / "out").exists())

    def test_six_video_requests_keep_full_workflow_calling_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in API_PROVIDERS:
                data = payload(root, name)
                data.update(workflow_mode="translation_picture_final", gates={"picture_and_mux_30m_human_accepted": True})
                request = FullWorkflowRequest.from_mapping(data)
                self.assertEqual(name, request.translation.provider.id)


class VideoApiRunnerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("translation_flow.runner.ToolPaths", FakeTools)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_six_video_backends_reach_real_shared_pipeline_without_network(self):
        for name in API_PROVIDERS:
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                request = JobRequest.from_mapping(payload(root, name))
                harness = Harness(name)
                callback = mock.Mock()
                with mock.patch.dict(os.environ, {"SUBTITLE_API_KEY": "fixture-key"}):
                    result = harness.runner(request, on_asr_ready=callback).run()
                callback.assert_called_once_with()
                report = json.loads((request.output_dir / ".translation-flow" / request.job_id / "translation.report.json").read_text())
                self.assertEqual(2, report["concurrency"])
                self.assertEqual(4, report["translated_count"])
                self.assertEqual(10, len(harness.requests))  # probe + context + 4 initial + 4 polish
                self.assertEqual("passed", result["technical_validation"]["status"])
                for url, body in harness.requests:
                    self.assertEqual(resolve_endpoint(name).url, url)
                    self.assertEqual(request.provider.model, body["model"])
                    self.assertNotIn("fixture-key", json.dumps(body))
                initial_positions = [i for i, p in enumerate(harness.successes) if p[0] == "initial"]
                polish_positions = [i for i, p in enumerate(harness.successes) if p[0] == "polish"]
                self.assertLess(max(initial_positions), min(polish_positions))
                for path in request.output_dir.rglob("*"):
                    if path.is_file() and path.suffix != ".mkv":
                        self.assertNotIn("fixture-key", path.read_text(encoding="utf-8"))
                self.assertEqual(b"read-only source fixture, not media", request.input_video.read_bytes())

    def test_regional_endpoint_and_token_cap_reach_wire_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            request = JobRequest.from_mapping(payload(Path(temp), endpoint_profile="qwen-intl", max_output_tokens=16384))
            harness = Harness("qwen")
            result = harness.runner(request).run()
            for url, body in harness.requests:
                self.assertEqual(resolve_endpoint("qwen", "qwen-intl").url, url)
                self.assertEqual(16384, body["max_tokens"])
            self.assertEqual(vars(request.provider), result["provider"])

    def test_complete_result_keyless_reuse_starts_no_business_service(self):
        for name in API_PROVIDERS[1:]:
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as temp:
                request = JobRequest.from_mapping(payload(Path(temp), name))
                harness = Harness(name)
                first = harness.runner(request).run()
                before = {p: p.read_bytes() for p in request.output_dir.rglob("*") if p.is_file() and p.name not in {"job.result.json", "events.jsonl", "timing.summary.json"}}
                timing_path = request.output_dir / ".translation-flow" / request.job_id / "timing.summary.json"
                first_timing = json.loads(timing_path.read_text(encoding="utf-8"))
                forbidden = mock.Mock(side_effect=AssertionError("must not start"))
                runner = harness.runner(request, key="")
                runner.asr_factory = runner.api_provider_factory = runner.translator = runner.packager_factory = forbidden
                result = runner.run()
                forbidden.assert_not_called()
                self.assertEqual(first["outputs"], result["outputs"])
                self.assertTrue(all(result["stages"][s]["reused"] for s in ("asr", "translation", "packaging")))
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                timing = json.loads(timing_path.read_text(encoding="utf-8"))
                self.assertEqual(2, len(timing["attempts"]))
                self.assertEqual(first_timing["attempts"][0], timing["attempts"][0])
                self.assertEqual("job_completed", timing["attempts"][1]["terminal_event"])

    def test_packaging_failure_resumes_from_srt_without_key_or_asr(self):
        with tempfile.TemporaryDirectory() as temp:
            request = JobRequest.from_mapping(payload(Path(temp)))
            harness = Harness("qwen")
            harness.fail_package = True
            with self.assertRaisesRegex(RuntimeError, "packaging"):
                harness.runner(request).run()
            calls = len(harness.requests)
            harness.fail_package = False
            result = harness.runner(request, key="").run()
            self.assertEqual(calls, len(harness.requests))
            self.assertEqual(1, harness.asr_calls)
            self.assertTrue(result["stages"]["translation"]["reused"])
            self.assertFalse(result["stages"]["packaging"]["reused"])

    def test_failed_api_batch_reuses_asr_and_successful_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            request = JobRequest.from_mapping(payload(Path(temp), "kimi"))
            harness = Harness("kimi")
            harness.fail_polish = True
            with self.assertRaises(FatalProviderError):
                harness.runner(request).run()
            self.assertFalse(list(request.output_dir.glob("*.zh-CN.srt")))
            forbidden = mock.Mock(side_effect=AssertionError("no new work without key"))
            runner = harness.runner(request, key="")
            runner.asr_factory = runner.api_provider_factory = forbidden
            with self.assertRaisesRegex(ValueError, "API Key"):
                runner.run()
            forbidden.assert_not_called()
            harness.fail_polish = False
            harness.runner(request).run()
            self.assertEqual(1, harness.asr_calls)
            counts = Counter(x for x in harness.successes if x[0] != "probe")
            self.assertTrue(all(count == 1 for count in counts.values()), counts)
            self.assertEqual(9, len(counts))

    def test_changed_configuration_or_media_cannot_reuse_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = JobRequest.from_mapping(payload(root))
            harness = Harness("qwen")
            harness.runner(request).run()
            for changes in ({"model": "other-model"}, {"endpoint_profile": "qwen-intl"},
                            {"max_output_tokens": 16384}):
                changed = JobRequest.from_mapping(payload(root, **changes))
                callback = mock.Mock()
                with self.assertRaisesRegex(ValueError, "检查点"):
                    harness.runner(changed, key="", on_asr_ready=callback).run()
                callback.assert_not_called()
            callback = mock.Mock()
            request.input_video.write_bytes(b"changed source fixture")
            with self.assertRaisesRegex(ValueError, "source or timeline"):
                harness.runner(request, key="", on_asr_ready=callback).run()
            callback.assert_not_called()
            self.assertEqual(10, len(harness.requests))

    def test_asr_resume_keeps_existing_a_backbone_checks_before_callback(self):
        with tempfile.TemporaryDirectory() as temp:
            request = JobRequest.from_mapping(payload(Path(temp)))
            harness = Harness("qwen")
            work = request.output_dir / ".translation-flow" / request.job_id / "asr"
            fusion, report = harness.run(request.input_video, work)
            write_srt(fusion, [replace(cue, start_ms=cue.start_ms + 500, end_ms=cue.end_ms + 500)
                               for cue in parse_srt(fusion)])
            callback = mock.Mock()
            with self.assertRaisesRegex(ValueError, "timecode mismatch"):
                harness.runner(request, on_asr_ready=callback).run()
            callback.assert_not_called()
            self.assertEqual([], harness.requests)

    def test_changed_srt_or_missing_identity_stops_before_callback(self):
        for corruption in ("subtitle", "identity"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temp:
                request = JobRequest.from_mapping(payload(Path(temp)))
                harness = Harness("qwen")
                result = harness.runner(request).run()
                if corruption == "subtitle":
                    srt = Path(result["outputs"]["chinese_srt"])
                    srt.write_text(srt.read_text(encoding="utf-8").replace("早上好", "不同文本"), encoding="utf-8")
                else:
                    report_path = request.output_dir / ".translation-flow" / request.job_id / "translation.report.json"
                    report = json.loads(report_path.read_text())
                    report.pop("provider_identity")
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                callback = mock.Mock()
                with self.assertRaisesRegex(ValueError, "检查点"):
                    harness.runner(request, key="", on_asr_ready=callback).run()
                callback.assert_not_called()

    def test_formal_video_cli_parses_selected_api_and_environment_key(self):
        from translation_flow.cli import main
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "request.json"
            path.write_text(json.dumps(payload(root, "anthropic", max_output_tokens=4096)), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SUBTITLE_API_KEY": "fixture-key"}), mock.patch(
                "translation_flow.cli.TranslationFlowRunner") as factory:
                self.assertEqual(0, main(["--request", str(path)]))
                request, key = factory.call_args.args
                self.assertEqual("anthropic", request.provider.id)
                self.assertEqual(4096, request.provider.max_output_tokens)
                self.assertEqual("fixture-key", key)
                self.assertNotIn("SUBTITLE_API_KEY", os.environ)
                self.assertNotIn("fixture-key", path.read_text())

    def test_optional_delivery_is_owned_by_original_job_or_existing_full_caller(self):
        for full_caller in (False, True):
            with self.subTest(full_caller=full_caller), tempfile.TemporaryDirectory() as temp:
                request = replace(JobRequest.from_mapping(payload(Path(temp))), additional_mp4=True)
                harness = Harness("qwen")
                callback = mock.Mock() if full_caller else None
                with mock.patch("translation_flow.codex_runner._append_mp4") as delivery:
                    runner = harness.runner(request, on_asr_ready=callback)
                    result = runner.run()
                    if full_caller:
                        delivery.assert_not_called()
                    else:
                        delivery.assert_called_once_with(runner, result)


class VideoApiFullCallerTests(unittest.TestCase):
    setUp = VideoApiRunnerTests.setUp
    def full_runner(self, root, harness, key="fixture-key"):
        data = payload(root, harness.name)
        data.update(workflow_mode="translation_picture_final", gates={"picture_and_mux_30m_human_accepted": True})
        request = FullWorkflowRequest.from_mapping(data)
        calls = []
        base = full_fixture.FullWorkflowTests()._runner(root, calls, [])
        def factory(request, api_key, **kwargs):
            return harness.runner(request, api_key, **kwargs)
        runner = FullWorkflowRunner(
            request, key, translation_runner_factory=factory, picture_runner=base.picture_runner,
            final_mux_factory=base.final_mux_factory, acceptance_gate_report=root / "accepted.json",
            emitter=EventEmitter(request.translation.job_id, root / "full-events.jsonl", stdout=io.StringIO()),
        )
        return runner, calls

    def test_full_caller_failure_then_keyless_reuses_completed_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = Harness("qwen")
            runner, calls = self.full_runner(root, harness)
            runner.picture_runner = mock.Mock(side_effect=RuntimeError("synthetic sibling failure"))
            with self.assertRaisesRegex(RuntimeError, "sibling"):
                runner.run()
            api_calls = len(harness.requests)
            resumed, calls = self.full_runner(root, harness, key="")
            resumed.translation_runner_factory = mock.Mock(side_effect=AssertionError("translation must be reused"))
            result = resumed.run()
            self.assertTrue(result["stages"]["translation"]["reused"])
            self.assertEqual(api_calls, len(harness.requests))
            self.assertEqual(["picture", "final_mux"], calls)

    def test_full_caller_manifest_covers_endpoint_and_output_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner, _ = self.full_runner(root, Harness("qwen"))
            manifest = runner._load_or_create_manifest()
            self.assertEqual(vars(runner.request.translation.provider), manifest["provider"])
            for change in ({"max_output_tokens": 16000}, {"endpoint_profile": "qwen-intl", "host": "dashscope-intl.aliyuncs.com"},
                           {"model": "other-model"}):
                original = runner.request
                provider = replace(original.translation.provider, **change)
                runner.request = replace(original, translation=replace(original.translation, provider=provider))
                with self.assertRaisesRegex(ValueError, "不匹配"):
                    runner._load_or_create_manifest()
                runner.request = original

    def test_full_caller_rejects_wrong_identity_before_either_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = Harness("openai")
            runner, _ = self.full_runner(root, harness)
            runner.run()
            report_path = runner._translation_result_path().parent / "translation.report.json"
            report = json.loads(report_path.read_text())
            report["provider_identity"] = "wrong-endpoint"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            runner.api_key = ""
            forbidden = mock.Mock(side_effect=AssertionError("must stop before branches"))
            runner.translation_runner_factory = runner.picture_runner = forbidden
            with self.assertRaisesRegex(ValueError, "无法复用"):
                runner.run()
            forbidden.assert_not_called()

    def test_full_caller_keyless_recovers_completed_srt_after_packaging_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness = Harness("gemini")
            harness.fail_package = True
            runner, _ = self.full_runner(root, harness)
            with self.assertRaisesRegex(RuntimeError, "packaging"):
                runner.run()
            api_calls = len(harness.requests)
            harness.fail_package = False
            resumed, _ = self.full_runner(root, harness, key="")
            result = resumed.run()
            self.assertEqual("technical_checks_passed", result["status"])
            self.assertEqual(api_calls, len(harness.requests))
            self.assertEqual(1, harness.asr_calls)


if __name__ == "__main__":
    unittest.main()
