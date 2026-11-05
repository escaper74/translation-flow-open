from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from translation_flow.asr import AsrPipeline, ToolPaths, load_completed_asr
from translation_flow.codex_runner import _job_identity
from translation_flow.contracts import JobRequest, ProviderRequest
from translation_flow.diagnostics import inspect_environment
from translation_flow.events import EventEmitter
from translation_flow.runner import TranslationFlowRunner
from translation_flow.srt import SubtitleCue, parse_srt, write_srt
from translation_flow.task_queue import request_info
from translation_flow.translation.provider import DeepSeekProvider, FatalProviderError
from translation_flow.translation.codex_provider import CodexProvider
from translation_flow.translation.text_check import prepare_text_checked_source, validate_text_check
import test_asr_packaging as asr_fixture
import test_full_workflow as full_fixture
from tests.test_codex_integration import AVAILABLE, FakeCodex, FakePackager, FakeTools
from tests.test_video_api_translation import Harness, payload
from tests.test_api_providers import Response, envelope


TEXTS = ["明日の会議は九時です。", "ご視聴ありがとうございました。", "資料を持ってきてください。",
         "ご視聴ありがとうございました。", "ご視聴ありがとうございました。",
         "はい、わかりました。", "はい、わかりました。", "はい、わかりました。"]
REMOVE = {2, 4, 5}


def decision(items):
    ids = {row["id"] for row in items}
    return {"remove_ids": sorted(REMOVE & ids), "uncertain_ids": sorted({6, 7, 8} & ids),
            "note": "这些重复应答可能是真实对白，保留。"}


def source_srt(root, texts=TEXTS):
    return write_srt(root / "source.srt", [SubtitleCue(i, i * 1000, i * 1000 + 900, text)
                                          for i, text in enumerate(texts, 1)])


class TextProvider:
    provider_id = "fixture"
    cache_identity = "fixture:local"

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        items = json.loads(messages[-1]["content"])["items"]
        self.calls.append(items)
        return json.dumps(decision(items))


class TextCheckTests(unittest.TestCase):
    def codex_adapter(self, root, reply, observed):
        class Process:
            returncode = 0

            def __init__(self, args, **kwargs):
                self.output = Path(args[args.index("--output-last-message") + 1])
                self.schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))

            def communicate(self, input=None, timeout=None):
                observed.append({"schema": self.schema, "instruction": input})
                # Honor the real adapter's requested shape, as the CLI does.
                response = ({"items": [{"id": 2, "text": "中性测试文本", "note": ""}]}
                            if "items" in self.schema["properties"] else reply)
                self.output.write_text(json.dumps(response), encoding="utf-8")
                return json.dumps({"type": "turn.completed"}), ""

        return CodexProvider(audit_dir=root / "calls", executable="unused-fixture.exe",
                             status_probe=lambda **_: AVAILABLE, process_factory=Process)

    def test_codex_real_adapter_accepts_decisions_and_reuses_reference(self):
        for removed in ([], sorted(REMOVE)):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as raw:
                root, observed = Path(raw), []
                source = source_srt(root)
                original = source.read_bytes()
                provider = self.codex_adapter(root, {"remove_ids": removed, "uncertain_ids": [], "note": ""}, observed)
                output, report = prepare_text_checked_source(source, {"asr_mode": "a_only"}, root / "check",
                                                              provider, "cli-default")
                self.assertEqual([cue for cue in parse_srt(source) if cue.index not in removed], parse_srt(output))
                self.assertEqual(removed, report["remove_ids"])
                self.assertEqual({"remove_ids", "uncertain_ids", "note"}, set(observed[0]["schema"]["required"]))
                self.assertNotIn('返回 JSON {"items":', observed[0]["instruction"])
                prepare_text_checked_source(source, {"asr_mode": "a_only"}, root / "check", provider, "cli-default")
                self.assertEqual(1, len(observed))
                self.assertEqual(original, source.read_bytes())

    def test_codex_real_adapter_keeps_source_on_invalid_decision(self):
        with tempfile.TemporaryDirectory() as raw:
            root, observed = Path(raw), []
            source = source_srt(root)
            original = source.read_bytes()
            provider = self.codex_adapter(root, {"remove_ids": [1], "uncertain_ids": [], "note": ""}, observed)
            output, report = prepare_text_checked_source(source, {}, root / "check", provider, "cli-default")
            self.assertEqual(2, len(observed))
            self.assertEqual({"remove_ids", "uncertain_ids", "note"}, set(observed[0]["schema"]["required"]))
            self.assertEqual(original, source.read_bytes())
            self.assertEqual(parse_srt(source), parse_srt(output))
            self.assertEqual([], report["remove_ids"])
            self.assertTrue(report["warnings"])

    def test_deepseek_adapter_keeps_json_object_request_for_text_check(self):
        with tempfile.TemporaryDirectory() as raw:
            root, requests = Path(raw), []

            def opener(request, timeout):
                body = json.loads(request.data)
                requests.append(body)
                items = json.loads(body["messages"][-1]["content"])["items"]
                return Response({"choices": [{"message": {"content": json.dumps(decision(items))}}]})

            provider = DeepSeekProvider("offline-fixture", opener=opener)
            source = source_srt(root)
            output, _ = prepare_text_checked_source(source, {"asr_mode": "a_c"}, root / "check",
                                                    provider, provider.default_model)
            self.assertEqual([1, 3, 6, 7, 8], [cue.index for cue in parse_srt(output)])
            self.assertEqual(1, len(requests))
            self.assertEqual({"type": "json_object"}, requests[0]["response_format"])
            self.assertNotIn("response_schema", requests[0])

    def test_remove_only_selected_repetitions_keep_uncertain_original_ids_and_times(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            source = source_srt(root)
            original = source.read_bytes()
            reviewed, report = prepare_text_checked_source(source, {}, root / "check", provider, "test")
            self.assertEqual([cue for cue in parse_srt(source) if cue.index not in REMOVE], parse_srt(reviewed))
            self.assertEqual([6, 7, 8], report["uncertain_ids"])
            self.assertTrue(report["warnings"])
            self.assertEqual({2, 4, 5, 6, 7, 8}, {i["id"] for i in provider.calls[0]})
            self.assertEqual(TEXTS[0], provider.calls[0][0]["before"])
            self.assertEqual(original, source.read_bytes())
            validate_text_check(source, reviewed, report, provider_id="fixture", model="test")
            prepare_text_checked_source(source, {}, root / "check", provider, "test")
            self.assertEqual(1, len(provider.calls))

    def test_normal_text_passes_without_model_review(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            source = source_srt(root, [TEXTS[0], TEXTS[2]])
            output, report = prepare_text_checked_source(source, {"asr_mode": "a_only"}, root / "check", provider, "test")
            self.assertEqual(parse_srt(source), parse_srt(output))
            self.assertEqual(0, report["candidate_count"])
            self.assertEqual([], provider.calls)

    def test_real_repeated_dialogue_is_retained_when_model_says_keep(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            provider.chat = Mock(return_value=json.dumps({"remove_ids": [], "uncertain_ids": [], "note": ""}))
            source = source_srt(root, ["はい、わかりました。"] * 14)
            output, report = prepare_text_checked_source(source, {}, root / "check", provider, "test")
            self.assertEqual(parse_srt(source), parse_srt(output))
            provider.chat.assert_called_once()

    def test_interrupted_check_reuses_successful_batch(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            source = source_srt(root, [TEXTS[1]] * 45)
            keep = json.dumps({"remove_ids": [], "uncertain_ids": [], "note": ""})
            provider.chat = Mock(side_effect=[keep, FatalProviderError("offline fixture failure")])
            with self.assertRaises(FatalProviderError):
                prepare_text_checked_source(source, {}, root / "check", provider, "test")
            provider.chat = Mock(return_value=keep)
            output, _ = prepare_text_checked_source(source, {}, root / "check", provider, "test")
            provider.chat.assert_called_once()
            self.assertEqual(5, len(json.loads(provider.chat.call_args.args[0][-1]["content"])["items"]))
            self.assertEqual(45, len(parse_srt(output)))

    def test_invalid_model_ids_do_not_delete_or_rewrite_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            source = source_srt(root)
            original = source.read_bytes()
            provider.chat = Mock(return_value='{"remove_ids":[1],"uncertain_ids":[],"note":""}')
            output, report = prepare_text_checked_source(source, {}, root / "check", provider, "test")
            self.assertEqual(2, provider.chat.call_count)
            self.assertEqual(original, source.read_bytes())
            self.assertEqual(parse_srt(source), parse_srt(output))
            self.assertEqual([], report["remove_ids"])
            self.assertTrue(report["warnings"])

    def test_all_hallucinations_leave_evidence_instead_of_fake_empty_subtitles(self):
        with tempfile.TemporaryDirectory() as raw:
            root, provider = Path(raw), TextProvider()
            source = source_srt(root, [TEXTS[1]] * 3)
            provider.chat = Mock(return_value='{"remove_ids":[1,2,3],"uncertain_ids":[],"note":""}')
            with self.assertRaisesRegex(ValueError, "没有可用对白"):
                prepare_text_checked_source(source, {}, root / "check", provider, "test")
            self.assertTrue(source.is_file())
            report = json.loads(next((root / "check").rglob("text-check.json")).read_text(encoding="utf-8"))
            self.assertEqual("no_usable_text", report["status"])


class ModeTools(FakeTools):
    @classmethod
    def from_root(cls, *args, **kwargs):
        result = cls()
        result.asr_mode = kwargs.get("asr_mode", "a_c")
        return result


class ModeAsr:
    def __init__(self, tools):
        self.mode = tools.asr_mode

    def run(self, source, work, emit=None, *, cancelled=None):
        fusion = source_srt(work / "fusion")
        fusion.rename(work / "fusion/fusion.ja.srt")
        fusion = work / "fusion/fusion.ja.srt"
        cues = parse_srt(fusion)
        write_srt(work / "A/A.ja.srt", cues)
        if self.mode == "a_c":
            write_srt(work / "C/C.ja.srt", cues)
        stat = source.stat()
        report = {"status": "complete", "source": str(source), "source_size_bytes": stat.st_size,
                  "source_mtime_ns": stat.st_mtime_ns, "asr_mode": self.mode, "a_cues": len(cues),
                  "c_cues": len(cues) if self.mode == "a_c" else 0, "fusion_cues": len(cues),
                  "timeline_preserved_from_a": True, "fusion_srt": str(fusion)}
        (work / "asr.report.json").write_text(json.dumps(report), encoding="utf-8")
        return fusion, report


class RepetitionCodex(FakeCodex):
    def chat(self, messages, **kwargs):
        if messages[0]["content"].startswith("只检查给出的日文"):
            self.calls.append(messages)
            return json.dumps(decision(json.loads(messages[-1]["content"])["items"]))
        return super().chat(messages, **kwargs)


class RepetitionApi(Harness):
    def opener(self, request, timeout):
        messages = json.loads(request.data)["messages"]
        if messages[0]["content"].startswith("只检查给出的日文"):
            self.requests.append((request.full_url, json.loads(request.data)))
            return Response(envelope(self.name, json.dumps(decision(json.loads(messages[-1]["content"])["items"]))))
        return super().opener(request, timeout)


class AsrModeTests(unittest.TestCase):
    def test_default_identity_and_queue_mode_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = payload(root, "codex")
            request = JobRequest.from_mapping(data)
            self.assertEqual("a_c", request.asr_mode)
            self.assertNotIn("asr_mode", _job_identity(request))
            single = JobRequest.from_mapping({**data, "asr_mode": "a_only"})
            self.assertEqual("a_only", _job_identity(single)["asr_mode"])
            path = single.output_dir / ".translation-flow" / single.job_id / "request.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({**data, "asr_mode": "a_only"}), encoding="utf-8")
            self.assertEqual("translation_flow.cli", request_info(path)["module"])
            with self.assertRaises(ValueError):
                JobRequest.from_mapping({**data, "asr_mode": "unknown"})

    def test_a_only_needs_no_c_tools_runs_no_c_or_repetition_gate_and_resumes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tools = replace(asr_fixture.AsrPipelineTests()._tools(root), asr_mode="a_only")
            tools = replace(tools, faster_python=root / "absent-python", faster_model=root / "absent-model",
                            transcribe_c=root / "absent-script", validate_quality=root / "absent-quality")
            source = root / "movie.mkv"
            source.write_bytes(b"not media")
            calls, events = [], []
            def process(args, **kwargs):
                calls.append(args)
                preflight = asr_fixture._input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if args[1] == str(tools.transcribe_a):
                    text = source_srt(root, [TEXTS[1]] * 14).read_text(encoding="utf-8")
                    for flag in ("--output-srt", "--raw-output-srt"):
                        Path(args[args.index(flag) + 1]).write_text(text, encoding="utf-8")
                elif args[1] == str(tools.clean_srt):
                    Path(args[args.index("--output") + 1]).write_bytes(Path(args[args.index("--input") + 1]).read_bytes())
                elif args[1] == str(tools.validate_structure):
                    Path(args[args.index("--report") + 1]).write_text('{"failures":[],"warnings":[]}', encoding="utf-8")
                else:
                    self.fail("unexpected subprocess")
                return asr_fixture._completed(args)
            pipeline = AsrPipeline(tools, process_runner=process)
            output, report = pipeline.run(source, root / "job", emit=lambda e, **p: events.append((e, p)))
            self.assertEqual(14, len(parse_srt(output)))
            self.assertEqual("skipped", report["c_status"])
            self.assertFalse((root / "job/C").exists())
            self.assertFalse(any(p.get("stage") == "asr_c" for e, p in events))
            count = len(calls)
            pipeline.run(source, root / "job")
            self.assertEqual(count, len(calls))
            with self.assertRaisesRegex(ValueError, "different recognition mode"):
                load_completed_asr(source, root / "job", asr_mode="a_c")
            with self.assertRaisesRegex(ValueError, "different source"):
                original_tools = replace(tools, asr_mode="a_c", faster_python=root / "faster-python.exe",
                                         faster_model=root / "faster-model", transcribe_c=root / "c.py")
                AsrPipeline(original_tools, process_runner=Mock()).run(source, root / "job")

    def test_offline_inventory_does_not_require_c_when_off(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chain = ToolPaths.from_root(root, root / "asr-lab", asr_mode="a_only", a_model_id="large-v3-turbo")
            for path in chain.required_paths():
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(b"not executed")
            report = inspect_environment(mode="video", provider="deepseek", tools=root, asr_mode="a_only")
            self.assertEqual("paths_available", report["status"])
            self.assertEqual([], report["optional_checks"])
            self.assertFalse(any(row["name"] in {"faster_python", "faster_model", "transcribe_c", "validate_quality"}
                                 for row in report["checks"]))

    def test_both_modes_codex_and_api_full_pipeline_remove_and_resume_offline(self):
        for provider in ("codex", "qwen"):
            for mode in ("a_c", "a_only"):
                with self.subTest(provider=provider, mode=mode), tempfile.TemporaryDirectory() as raw, patch(
                    "translation_flow.runner.ToolPaths", ModeTools
                ), patch("sys.stdout", io.StringIO()):
                    root, calls = Path(raw), []
                    full = full_fixture.FullWorkflowTests()._runner(root, calls, [])
                    config = JobRequest.from_mapping(payload(root, provider)).provider
                    full.request = replace(full.request, translation=replace(full.request.translation, provider=config, asr_mode=mode))
                    api = RepetitionApi("qwen")
                    FakeCodex.calls = []
                    full.translation_runner_factory = lambda *a, **kw: TranslationFlowRunner(
                        *a, asr_factory=ModeAsr, packager_factory=FakePackager,
                        codex_provider_factory=RepetitionCodex, api_provider_factory=api.provider, **kw)
                    result = full.run()
                    child = json.loads(full._translation_result_path().read_text(encoding="utf-8"))
                    self.assertEqual("technical_checks_passed", result["status"])
                    self.assertEqual([1, 3, 6, 7, 8], [cue.index for cue in parse_srt(child["outputs"]["chinese_srt"])])
                    self.assertEqual(8, len(parse_srt(child["asr"]["fusion_srt"])))
                    if mode == "a_only":
                        events = [json.loads(line) for line in full.events.events_path.read_text(encoding="utf-8").splitlines()]
                        self.assertFalse(any("A+C" in event.get("detail", "") for event in events))
                        self.assertTrue(full._manifest_payload()["gates"]["picture_gpu_after_asr_a"])
                    count = len(FakeCodex.calls) + len(api.requests)
                    full.run()
                    self.assertEqual(count, len(FakeCodex.calls) + len(api.requests))
                    self.assertEqual(1, calls.count("final_mux"))


@unittest.skipUnless(os.name == "nt", "native Windows desktop")
class AsrModeUiTests(unittest.TestCase):
    def test_wpf_checkbox_default_off_request_and_new_task_reset(self):
        from tests.test_frontend_presentation import FrontendPresentationTests
        ui = FrontendPresentationTests()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.mp4").write_bytes(b"not media")
            with ui.isolated_queue(root) as (project, state, start, worker):
                ui.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    if (-not $UseCCheckBox.IsChecked -or $script:CurrentAsrMode -ne 'a_c') { throw 'default changed' }
    Select-ComboBoxItemByTag $TaskModeBox 'translation'
    Set-SelectedFiles @('TESTROOT\source.mp4')
    $OutputDirectoryBox.Text='TESTROOT\out'
    $UseCCheckBox.IsChecked=$false
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1
    return
}
if ($script:QueueSnapshot.items.Count -ne 1 -or $script:QueueSnapshot.items[0].status -ne 'completed') { return }
$row=$script:QueueSnapshot.items[0]
$saved=Get-Content -LiteralPath $row.request_path -Raw | ConvertFrom-Json
if ($saved.asr_mode -ne 'a_only') { throw 'unchecked mode not saved' }
$resume=Get-TaskCenterResumeRequest $row.request_path
if ($resume.AsrMode -ne 'a_only') { throw 'mode lost on resume' }
$NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if (-not $UseCCheckBox.IsChecked -or $script:CurrentAsrMode -ne 'a_c') { throw 'new task default not restored' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root)), project=project, state=state)
