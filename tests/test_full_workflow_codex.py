from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_full_workflow as full_fixtures
from test_codex_integration import FakeAsr, FakeCodex, FakePackager, FakeTools
from test_codex_source_review import write_asr
from translation_flow.contracts import ProviderRequest
from translation_flow.runner import TranslationFlowRunner
from translation_flow.srt import parse_srt, subtitle_digest, write_srt
from translation_flow.translation.codex_provider import CodexQuotaExceeded, TranslationCancelled


class ChangedAsr(FakeAsr):
    def run(self, source, root, emit, *, cancelled=None):
        type(self).calls += 1
        fused, report, _ = write_asr(root.parent,
                                   ["明日の会議です。", "資料を持っていきます。"],
                                   ["明日の鍵です。", "資料を持っていきます。"])
        stat = source.stat()
        report.update(source=str(source), source_size_bytes=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                      a_cues=2, c_cues=2, fusion_cues=2, timeline_preserved_from_a=True)
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return fused, report


class ReviewCodex(FakeCodex):
    def chat(self, messages, **kwargs):
        if kwargs.get("json_output"):
            content = messages[-1]["content"]
            items = json.JSONDecoder().raw_decode(content[content.rfind('{"items":'):])[0]["items"]
            if items and "a" in items[0]:
                self.check_cancelled()
                self.calls.append(messages)
                return json.dumps({"items": [{"id": row["id"], "text": row["a"], "note": "按上下文保守采用 A。"} for row in items]})
        return super().chat(messages, **kwargs)


class FullWorkflowCodexTests(unittest.TestCase):
    def setUp(self):
        FakeCodex.calls = []
        ChangedAsr.calls = 0

    def runner(self, root, calls, *, provider=ReviewCodex, asr=ChangedAsr, packager=FakePackager, mp4=False):
        runner = full_fixtures.FullWorkflowTests()._runner(root, calls, [], additional_mp4=mp4)
        runner.request = replace(runner.request, translation=replace(
            runner.request.translation, provider=ProviderRequest("codex", "chatgpt.com", "cli-default")))
        runner.api_key = ""
        picture = runner.picture_runner

        def retained_picture(config, **kwargs):
            report_path = config.work_dir / "reports/workflow-report.json"
            if report_path.is_file():
                calls.append("picture_reused")
                return {**json.loads(report_path.read_text(encoding="utf-8")), "reused": True}
            return picture(config, **kwargs)

        runner.picture_runner = retained_picture
        runner.translation_runner_factory = lambda *args, **kwargs: TranslationFlowRunner(
            *args, codex_provider_factory=provider, asr_factory=asr, packager_factory=packager, **kwargs)
        return runner

    def test_reviewed_source_full_workflow_and_mp4_resume_without_repeated_calls(self):
        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", FakeTools), patch(
            "translation_flow.codex_runner._append_mp4", side_effect=AssertionError("top level owns MP4")
        ), patch("sys.stdout", new_callable=io.StringIO):
            root, calls = Path(temp), []
            runner = self.runner(root, calls, mp4=True)
            picture_started = threading.Event()
            original_picture = runner.picture_runner

            def picture(*args, **kwargs):
                self.assertEqual(1, ChangedAsr.calls)
                picture_started.set()
                return original_picture(*args, **kwargs)

            class AfterPicture(ReviewCodex):
                def chat(self, *args, **kwargs):
                    if not picture_started.wait(3):
                        raise AssertionError("ASR callback must precede source-review calls")
                    return super().chat(*args, **kwargs)

            runner.picture_runner = picture
            runner.translation_runner_factory = lambda *args, **kwargs: TranslationFlowRunner(
                *args, codex_provider_factory=AfterPicture, asr_factory=ChangedAsr,
                packager_factory=FakePackager, **kwargs)
            result = runner.run()
            job_root = runner._translation_result_path().parent
            pointer = json.loads((job_root / "source-review.pointer.json").read_text(encoding="utf-8"))
            self.assertNotEqual(subtitle_digest(parse_srt(job_root / "asr/fusion/fusion.ja.srt")),
                                subtitle_digest(parse_srt(pointer["reviewed_srt"])))
            self.assertEqual("technical_checks_passed", result["status"])
            self.assertEqual(1, calls.count("final_mux"))
            self.assertEqual(1, calls.count("mp4_delivery"))
            original_calls = len(FakeCodex.calls)
            resumed = self.runner(root, calls, mp4=True, provider=Mock(side_effect=AssertionError("no CLI")),
                                  asr=Mock(side_effect=AssertionError("no ASR"))).run()
            self.assertTrue(resumed["stages"]["translation"]["reused"])
            self.assertEqual(original_calls, len(FakeCodex.calls))
            self.assertEqual(1, calls.count("final_mux"))
            self.assertEqual(1, calls.count("mp4_delivery"))

    def test_quota_failure_keeps_other_branch_and_resume_reuses_asr(self):
        class Quota(ReviewCodex):
            def chat(self, *args, **kwargs):
                raise CodexQuotaExceeded("quota")

        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", FakeTools), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            root, calls = Path(temp), []
            with self.assertRaises(CodexQuotaExceeded):
                self.runner(root, calls, provider=Quota).run()
            self.assertEqual(["picture"], calls)
            result = self.runner(root, calls, asr=Mock(side_effect=AssertionError("reuse ASR"))).run()
            self.assertEqual("technical_checks_passed", result["status"])
            self.assertEqual(1, ChangedAsr.calls)
            self.assertEqual(1, calls.count("picture"))

    def test_packaging_failure_resumes_without_codex_login_or_asr(self):
        broken = Mock()
        broken.return_value.package.side_effect = OSError("disk failure")
        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", FakeTools), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            root, calls = Path(temp), []
            with self.assertRaises(OSError):
                self.runner(root, calls, packager=broken).run()
            before = len(FakeCodex.calls)
            self.runner(root, calls, provider=Mock(side_effect=AssertionError("no CLI")),
                        asr=Mock(side_effect=AssertionError("no ASR"))).run()
            self.assertEqual(before, len(FakeCodex.calls))
            self.assertEqual(1, calls.count("picture"))

    def test_changed_checkpoint_is_rejected_before_any_branch(self):
        for changed in ("a", "c", "fusion", "reviewed", "chinese", "review_report"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp, patch(
                "translation_flow.runner.ToolPaths", FakeTools
            ), patch("sys.stdout", new_callable=io.StringIO):
                root, calls = Path(temp), []
                runner = self.runner(root, calls)
                runner.run()
                job_root = runner._translation_result_path().parent
                output = json.loads(runner._translation_result_path().read_text(encoding="utf-8"))["outputs"]
                path = {"a": job_root / "asr/A/A.ja.srt", "c": job_root / "asr/C/C.ja.srt",
                        "fusion": job_root / "asr/fusion/fusion.ja.srt",
                        "reviewed": Path(output["reviewed_japanese_srt"]),
                        "chinese": Path(output["chinese_srt"]),
                        "review_report": Path(output["source_review_report"])}[changed]
                if changed == "review_report":
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["status"] = "incomplete"
                    path.write_text(json.dumps(data), encoding="utf-8")
                else:
                    cues = parse_srt(path)
                    write_srt(path, [replace(cues[0], text="更改测试夹具"), *cues[1:]])
                calls.clear()
                with self.assertRaises(ValueError) as failure:
                    self.runner(root, calls).run()
                self.assertNotIn("API Key", str(failure.exception))
                self.assertEqual([], calls)

    def test_invalid_asr_timeline_does_not_release_callback_or_call_codex(self):
        class InvalidAsr(ChangedAsr):
            def run(self, *args, **kwargs):
                fused, report = super().run(*args, **kwargs)
                cues = parse_srt(fused)
                write_srt(fused, [replace(cues[0], start_ms=100), *cues[1:]])
                return fused, report

        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", FakeTools), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            calls = []
            with self.assertRaises(ValueError):
                self.runner(Path(temp), calls, asr=InvalidAsr).run()
            self.assertEqual([], calls)
            self.assertEqual([], FakeCodex.calls)

    def test_cancel_preserves_started_branch_and_skips_final_mux(self):
        class Cancel(ReviewCodex):
            def chat(self, *args, **kwargs):
                raise TranslationCancelled("stopped")

        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.runner.ToolPaths", FakeTools), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            calls = []
            runner = self.runner(Path(temp), calls, provider=Cancel)
            with self.assertRaises(TranslationCancelled):
                runner.run()
            self.assertEqual(["picture"], calls)
            manifest = json.loads(runner.paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual("cancelled", manifest["status"])

    def test_full_cli_cancellation_exit_and_event(self):
        from full_workflow.cli import main
        with tempfile.TemporaryDirectory() as temp, patch("sys.stdout", new_callable=io.StringIO) as output:
            root = Path(temp)
            request = self.runner(root, []).request
            translation = request.translation
            payload = {"schema_version": 1, "workflow_mode": "translation_picture_final",
                       "job_id": translation.job_id, "input_video": str(translation.input_video),
                       "output_dir": str(translation.output_dir), "profile_id": translation.profile_id,
                       "provider": vars(translation.provider), "consent": vars(translation.consent),
                       "gates": {"picture_and_mux_30m_human_accepted": True}}
            path = root / "request.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("full_workflow.cli.FullWorkflowRunner.run", side_effect=TranslationCancelled("stopped")):
                self.assertEqual(2, main(["--request", str(path)]))
            self.assertEqual("job_cancelled", json.loads(output.getvalue())["event"])
