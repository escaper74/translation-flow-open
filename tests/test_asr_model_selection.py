from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from translation_flow.asr import AsrPipeline, ToolPaths, load_completed_asr
from translation_flow.contracts import JobRequest, ProviderRequest, RunConsent
from translation_flow.codex_runner import _job_identity
from translation_flow.events import EventEmitter
from translation_flow.runner import TranslationFlowRunner
from translation_flow.task_queue import request_info
import test_asr_packaging as asr_fixtures
from test_codex_integration import FakeAsr, FakeCodex, FakePackager, FakeTools


class AsrModelSelectionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "desktop contract requires PowerShell")
    def test_desktop_request_identity_and_resume_model(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mkv"
            source.write_bytes(b"fixture")
            command = r'''
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'desktop/Start-TranslationFlow.ps1'), [ref]$null, [ref]$null)
$names = @('Get-StableJobId','Start-ConfirmedRun','Get-SelectedTaskMode','Get-SelectedOutputTarget','Get-SelectedProviderSettings')
foreach ($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    if ($node.Name -in $names) { Invoke-Expression $node.Extent.Text }
}
. (Join-Path $repo 'desktop/TranslationFlowProvider.ps1')
. (Join-Path $repo 'desktop/TranslationFlowTaskCenter.ps1')
. (Join-Path $repo 'desktop/TranslationFlowQueueUi.ps1')
$OutputDirectoryBox = @{Text=$target}
$InputVideoBox = @{Text=$source}
$TaskModeBox = @{SelectedItem=@{Tag='translation'}}
$OutputFormatBox = @{SelectedItem=@{Tag='mkv'}}
$ProfileBox = @{SelectedItem=@{Tag='normal'}}
$ProviderBox = @{SelectedItem=@{Tag='codex'}}
$EndpointBox = @{SelectedItem=$null}
$ModelBox = @{Text='cli-default'}
$ToolRootBox = @{Text='unused'}
$script:ProviderId='codex'; $script:ProviderHost='chatgpt.com'; $script:ProviderModel='cli-default'
$script:CollectQueueRequests=$true
$script:QueueEnabled=$false
$script:CollectedQueueItems=[Collections.Generic.List[object]]::new()
$script:CurrentWhisperAModel='large-v3'
Start-ConfirmedRun
$original = $script:CollectedQueueItems[0].request_path
$legacyId=Get-StableJobId $source $target 'normal' 'codex' 'chatgpt.com' 'cli-default'
if ((Get-Content -LiteralPath $original -Raw | ConvertFrom-Json).job_id -ne $legacyId) { throw 'legacy ID changed' }
if ((Get-Content -LiteralPath $original -Raw | ConvertFrom-Json).PSObject.Properties.Name -contains 'asr_a_model') { throw 'default changed' }
$script:CurrentWhisperAModel='large-v3-turbo'
Start-ConfirmedRun
$candidate = $script:CollectedQueueItems[1].request_path
if ($candidate -eq $original) { throw 'model identities collided' }
$before=[IO.File]::ReadAllText($candidate)
$resume=Get-TaskCenterResumeRequest $candidate
if ($resume.AsrAModel -ne 'large-v3-turbo') { throw 'model lost on resume' }
$script:CurrentWhisperAModel=$resume.AsrAModel
Start-ConfirmedRun
if ([IO.File]::ReadAllText($candidate) -cne $before) { throw 'request changed on resume' }
$script:QueueEnabled=$true
$script:PendingQueueResumeId='fixture'
$script:QueueSnapshot=@{items=@([pscustomobject]@{id='fixture';request_path=$candidate})}
if (Test-QueueResumeSettings) { throw 'same model resume rejected' }
$script:CurrentWhisperAModel='large-v3'
if (-not (Test-QueueResumeSettings)) { throw 'changed model silently resumed' }
$script:QueueEnabled=$false
$script:PendingQueueResumeId=''
$legacyResume=Get-TaskCenterResumeRequest $original
if ($legacyResume.AsrAModel -ne 'large-v3') { throw 'legacy model changed to new UI default' }
$before=[IO.File]::ReadAllText($original)
Start-ConfirmedRun
if ([IO.File]::ReadAllText($original) -cne $before) { throw 'legacy request changed on resume' }
$script:CurrentWhisperAModel='large-v3-turbo'
$TaskModeBox.SelectedItem.Tag='translation_picture_final'
Start-ConfirmedRun
$full=Get-Content -LiteralPath $script:CollectedQueueItems[4].request_path -Raw | ConvertFrom-Json
if ($full.asr_a_model -ne 'large-v3-turbo' -or $full.workflow_mode -ne 'translation_picture_final') { throw 'full request lost model' }
$TaskModeBox.SelectedItem.Tag='srt_translation'
$script:PendingSrtRequestPath=''
Start-ConfirmedRun
$srt=Get-Content -LiteralPath $script:CollectedQueueItems[5].request_path -Raw | ConvertFrom-Json
if ($srt.PSObject.Properties.Name -contains 'asr_a_model') { throw 'SRT inherited ASR model' }
Write-Output 'passed'
'''
            prefix = f"$repo='{str(project).replace(chr(39),chr(39)*2)}';$source='{source}';$target='{root / 'out'}';"
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", prefix + command],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("passed", completed.stdout)

    def test_model_path_changes_only_a(self):
        baseline = ToolPaths.from_root("tools", "tools/asr-lab")
        turbo = ToolPaths.from_root("tools", "tools/asr-lab", a_model_id="large-v3-turbo")
        self.assertEqual("ggml-large-v3-turbo.bin", turbo.whisper_model.name)
        self.assertEqual(baseline.faster_model, turbo.faster_model)
        self.assertEqual(baseline.whisper, turbo.whisper)
        self.assertEqual("large-v3", baseline.a_model_id)
        with self.assertRaises(ValueError):
            ToolPaths.from_root("tools", "tools/asr-lab", a_model_id="unknown")

    def test_request_default_and_identity_backward_compatible(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "fixture.mp4"
            source.write_bytes(b"fixture")
            payload = {"schema_version": 1, "job_id": "test", "input_video": str(source),
                       "output_dir": str(root / "out"), "profile_id": "normal",
                       "provider": {"id": "codex", "host": "chatgpt.com", "model": "cli-default"},
                       "consent": {"current_run": True, "subtitle_text_only": True, "audio_video_local": True}}
            baseline = JobRequest.from_mapping(payload)
            self.assertEqual("large-v3", baseline.asr_a_model)
            self.assertNotIn("asr_a_model", _job_identity(baseline))
            turbo = JobRequest.from_mapping({**payload, "asr_a_model": "large-v3-turbo"})
            self.assertEqual("large-v3-turbo", _job_identity(turbo)["asr_a_model"])
            for bad in (None, {}, "../model", "turbo"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    JobRequest.from_mapping({**payload, "asr_a_model": bad})

            request_path = root / "out/.translation-flow/test/request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text(json.dumps({**payload, "asr_a_model": "large-v3-turbo"}), encoding="utf-8")
            self.assertEqual("translation_flow.cli", request_info(request_path)["module"])

    def test_partial_baseline_cannot_resume_as_turbo(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            tools = replace(asr_fixtures.AsrPipelineTests()._tools(root), a_model_id="large-v3-turbo")
            job = root / "job"
            job.mkdir()
            stat = source.stat()
            (job / "asr.input.json").write_text(json.dumps({"source": str(source),
                "source_size_bytes": stat.st_size, "source_mtime_ns": stat.st_mtime_ns}), encoding="utf-8")
            process = Mock(side_effect=AssertionError("must not run"))
            with self.assertRaisesRegex(ValueError, "different source"):
                AsrPipeline(tools, process_runner=process).run(source, job)
            process.assert_not_called()

    def test_turbo_codex_flow_resume_and_changed_model_rejected(self):
        class TurboAsr(FakeAsr):
            def run(self, source, root, emit, *, cancelled=None):
                fused, report = super().run(source, root, emit, cancelled=cancelled)
                report["a_model"] = "large-v3-turbo"
                Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
                return fused, report

        with tempfile.TemporaryDirectory() as raw, patch("translation_flow.runner.ToolPaths", FakeTools):
            root = Path(raw)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            request = JobRequest(1, "turbo", source, root / "out", "normal",
                ProviderRequest("codex", "chatgpt.com", "cli-default"), RunConsent(True, True, True),
                asr_a_model="large-v3-turbo")
            FakeCodex.calls = []
            def runner(req):
                return TranslationFlowRunner(req, asr_factory=TurboAsr, packager_factory=FakePackager,
                    codex_provider_factory=FakeCodex, emitter=EventEmitter(req.job_id,
                        root / "events.jsonl", stdout=io.StringIO()))
            completed = runner(request).run()
            self.assertEqual("large-v3-turbo", completed["asr"]["a_model"])
            calls = len(FakeCodex.calls)
            runner(request).run()
            self.assertEqual(calls, len(FakeCodex.calls))
            with self.assertRaisesRegex(ValueError, "身份已改变"):
                runner(replace(request, asr_a_model="large-v3")).run()
            with self.assertRaises(ValueError):
                load_completed_asr(source, request.output_dir / ".translation-flow/turbo/asr")
            self.assertEqual(calls, len(FakeCodex.calls))
