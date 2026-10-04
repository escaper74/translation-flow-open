from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from translation_flow.translation.api_providers import API_PROVIDERS, ENDPOINTS
from test_srt_codex_cli import FIXTURE

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "Start-TranslationFlow.ps1"


@unittest.skipUnless(os.name == "nt", "Windows desktop integration")
class SubtitleUiTests(unittest.TestCase):
    def powershell(self, *args, timeout=35):
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy",
                                 "Bypass", "-STA", *args], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                env={key.upper(): value for key, value in os.environ.items()})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_api_failure_guidance_does_not_misidentify_provider(self):
        values = self.powershell("-Command",
            f". '{ROOT / 'desktop' / 'TranslationFlowUiSummary.ps1'}';"
            "@((Get-UiFailureGuidance 'qwen rate_limit_exceeded' 'translation' 'translation'),"
            "(Get-UiFailureGuidance 'Missing API key' 'translation' 'translation'))|ConvertTo-Json -Compress")
        self.assertNotIn("Codex", json.dumps(values, ensure_ascii=False))
        self.assertIn("API Key", values[1]["summary"])

    def test_provider_regions_match_backend_and_restore_exact_configuration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.srt"
            source.write_bytes(FIXTURE.read_bytes())
            requests = []
            from urllib.parse import urlsplit
            for endpoint, config in ENDPOINTS.items():
                if config.provider == "deepseek":
                    continue
                job = root / ".srt-translation" / endpoint
                job.mkdir(parents=True)
                request = job / "request.json"
                request.write_text(json.dumps({"schema_version": 1, "job_id": endpoint,
                    "workflow_mode": "srt_translation", "input_srt": str(source), "output_dir": str(root),
                    "profile_id": "normal", "provider": {"id": config.provider,
                    "host": urlsplit(config.url).netloc, "model": "chosen-model",
                    "endpoint_profile": endpoint, "max_output_tokens": 2048}}), encoding="utf-8")
                requests.append(str(request))
            script = root / "probe.ps1"
            script.write_text(
                f". '{ROOT / 'desktop' / 'TranslationFlowTaskCenter.ps1'}'\n"
                "$paths=@(" + ",".join("'" + p + "'" for p in requests) + ")\n"
                "$values=@(foreach($path in $paths){$resume=Get-TaskCenterResumeRequest $path;"
                "[void](Request-TranslationFlowCancellation $path);$resume});"
                "$values|ConvertTo-Json -Depth 5 -Compress", encoding="utf-8-sig")
            values = self.powershell("-File", str(script))
            self.assertEqual(len(values), 7)
            for value in values:
                self.assertEqual(value["TaskMode"], "srt_translation")
                self.assertEqual(value["ProviderModel"], "chosen-model")
                self.assertEqual(value["MaxOutputTokens"], 2048)
                self.assertIn(value["EndpointProfile"], ENDPOINTS)
                self.assertTrue((Path(value["RequestPath"]).parent / "cancel.requested").exists())

    def test_wpf_srt_submission_completion_and_restore_through_real_bridge(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.srt"
            source.write_bytes(FIXTURE.read_bytes())
            package = root / "translation_flow"
            (root / "source.mkv").write_bytes(b"offline placeholder; never processed")
            (root / "config").mkdir()
            (root / "config" / "local.psd1").write_text(
                "@{PythonPath='Z:\\unused-python.exe';ToolRoot='Z:\\本机工具';"
                "JasnaPath='Z:\\existing-picture.exe';AcceptanceGateReport='Z:\\existing-gate.json'}",
                encoding="utf-8")
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "cli.py").write_text(
                "import json\nprint(json.dumps({'status':'available','message':'offline fixture'}))\n",
                encoding="utf-8")
            (package / "srt_desktop.py").write_text('''
import json, os, pathlib, sys
request_path=pathlib.Path(sys.argv[sys.argv.index('--request')+1])
request=json.loads(request_path.read_text(encoding='utf-8'))
job=request_path.parent
(job/'bridge-probe.json').write_text(json.dumps({'has_key':bool(os.environ.get('SUBTITLE_API_KEY')),
    'tool_root':os.environ.get('TRANSLATION_FLOW_TOOL_ROOT')}),encoding='utf-8')
output=job/'fixture.zh-CN.srt'
output.write_text('1\\n00:00:00,000 --> 00:00:02,000\\n会议开始。\\n',encoding='utf-8')
report=job/'translation.report.json'
report.write_text(json.dumps({'status':'passed'}),encoding='utf-8')
result={'outputs':{'chinese_srt':str(output),'technical_report':str(report)},
    'technical_validation':{'status':'passed','report_path':str(report)}}
(job/'job.result.json').write_text(json.dumps(result),encoding='utf-8')
events=[{'event':'job_started'},{'event':'job_completed',**result}]
for seq,item in enumerate(events,1):
    payload={'schema_version':1,'job_id':request['job_id'],'seq':seq,**item}
    line=json.dumps(payload)
    print(line,flush=True)
    with (job/'events.jsonl').open('a',encoding='utf-8') as f: f.write(line+'\\n')
''', encoding="utf-8")
            injection = r'''
# This fixture isolates the original request bridge from the service probe.
function Start-CheckedBatch { Start-AuthorizedBatch }
$script:UiTestState=0
$script:UiTestResult=$null
$script:UiTestStarted=[DateTime]::UtcNow
$script:UiTestTimer=New-Object System.Windows.Threading.DispatcherTimer
$script:UiTestTimer.Interval=[TimeSpan]::FromMilliseconds(100)
$script:UiTestTimer.Add_Tick({
    try {
        if (([DateTime]::UtcNow-$script:UiTestStarted).TotalSeconds -gt 15) { throw 'UI timeout' }
        if ($script:CodexStatus -eq 'checking') { return }
        if ($script:UiTestState -eq 0) {
            if ($ToolRootBox.Text -ne 'Z:\本机工具' -or $PythonPathBox.Text -eq 'Z:\unused-python.exe') { throw 'local paths or explicit Python precedence failed' }
            if ($env:TRANSLATION_FLOW_JASNA -ne 'Z:\existing-picture.exe' -or $env:TRANSLATION_FLOW_ACCEPTANCE_REPORT -ne 'Z:\existing-gate.json') { throw 'local runtime defaults missing' }
            Set-SelectedVideo (Join-Path $ProjectRoot 'source.srt')
            $OutputDirectoryBox.Text=$ProjectRoot
            if ((Get-SelectedTaskMode) -ne 'srt_translation' -or $OutputFormatBox.IsEnabled) { throw 'SRT mode selection failed' }
            foreach($id in @('deepseek','qwen','kimi','openai','anthropic','gemini','codex')) {
                $ApiKeyBox.Password='must-be-cleared'
                Select-ComboBoxItemByTag $ProviderBox $id
                if ($ApiKeyBox.Password) { throw 'provider switch retained old key' }
                if ((Get-SelectedTaskMode) -ne 'srt_translation') { throw 'provider switch lost SRT mode' }
                if ($id -eq 'codex') {
                    if ([string]$ApiKeyPanel.Visibility -ne 'Collapsed') { throw 'Codex key panel visible' }
                } else {
                    if ($id -ne 'deepseek') { $ModelBox.Text='explicit-'+$id }
                    $ApiKeyBox.Password='fixture-unused'
                    if (Test-RunInputs) { throw ('provider input failed: '+(Test-RunInputs)) }
                }
            }
            Select-ComboBoxItemByTag $ProviderBox 'qwen'
            $ModelBox.Text='chosen-qwen'
            $TokenCapBox.Text='2048'
            Select-ComboBoxItemByTag $EndpointBox 'qwen-intl'
            $ApiKeyBox.Password='fixture-unused'
            $ToolRootBox.Text=Join-Path $ProjectRoot 'existing tools'
            $TokenCapBox.Text='bad'
            if (-not (Test-RunInputs)) { throw 'invalid token limit accepted' }
            $TokenCapBox.Text='2048'
            Begin-ConfiguredRun
            if ([string]$ConsentOverlay.Visibility -ne 'Visible') { throw 'consent missing' }
            if ($ConsentDetailText.Text -notmatch 'dashscope-intl.aliyuncs.com' -or $ConsentDetailText.Text -notmatch 'chosen-qwen') { throw 'consent wrong endpoint or model' }
            $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
            if (-not $script:IsRunning -or $ApiKeyBox.Password) { throw 'start/key lifetime failed' }
            $script:UiTestState=1
            return
        }
        if ($script:IsRunning) { return }
        if ($null -eq $script:CompletedEvent -or $script:ProtocolFailure -or $null -ne $script:FailedEvent) { throw ('run did not complete: '+$StageText.Text) }
        if ($RecoverySummaryText.Text -notmatch '字幕结构检查') { throw 'SRT completion uses media claims' }
        $requestPath=$script:CurrentRequestPath
        $saved=[IO.File]::ReadAllText($requestPath,[Text.Encoding]::UTF8)|ConvertFrom-Json
        if ($saved.provider.endpoint_profile -ne 'qwen-intl' -or $saved.provider.max_output_tokens -ne 2048 -or $saved.consent.provider -ne 'qwen') { throw 'request lost provider settings' }
        $probe=Get-Content (Join-Path (Split-Path $requestPath) 'bridge-probe.json') -Raw|ConvertFrom-Json
        if (-not $probe.has_key -or $probe.tool_root -ne (Join-Path $ProjectRoot 'existing tools')) { throw 'child configuration missing' }
        Select-ComboBoxItemByTag $ProviderBox 'codex'
        Refresh-TaskCenter
        $TaskHistoryGrid.SelectedItem=$TaskHistoryGrid.Items[0]
        if ($TaskHistoryGrid.SelectedItem.ModeLabel -ne 'SRT 翻译') { throw 'task history mode wrong' }
        $ResumeTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        if ([string]$ConsentOverlay.Visibility -ne 'Visible' -or $InputErrorText.Text) { throw ('resume blocked: '+$InputErrorText.Text) }
        $restored=Get-SelectedProviderSettings
        if ($restored.Id -ne 'qwen' -or $restored.Model -ne 'chosen-qwen' -or $restored.EndpointProfile -ne 'qwen-intl' -or $restored.MaxOutputTokens -ne 2048) { throw 'resume changed provider configuration' }
        $srtOutput=$script:FinalMediaPath
        $CancelConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        Select-ComboBoxItemByTag $TaskModeBox 'translation'
        Set-SelectedVideo (Join-Path $ProjectRoot 'source.mkv')
        Select-ComboBoxItemByTag $OutputFormatBox 'mp4'
        $ApiKeyBox.Password='fixture-unused'
        function script:Start-TranslationFlowBackend { throw 'offline request capture; media not started' }
        Begin-ConfiguredRun
        $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        $videoRequest=$script:CurrentRequestPath
        if ($script:IsRunning -or $videoRequest -eq $requestPath) { throw 'video request capture failed' }
        $script:UiTestResult=@{status='passed';request=$requestPath;output=$srtOutput;video_request=$videoRequest}
    } catch { $script:UiTestResult=@{status='failed';error=$_.Exception.Message} }
    if ($null -ne $script:UiTestResult) { $script:UiTestTimer.Stop();$Window.Close() }
})
$script:UiTestTimer.Start()
[void]$Window.ShowDialog()
$script:UiTestResult|ConvertTo-Json -Compress
'''
            copied = UI.read_text(encoding="utf-8-sig").replace(
                "$PSScriptRoot", "'" + str(UI.parent).replace("'", "''") + "'")
            copied = copied.replace("[void]$Window.ShowDialog()", injection)
            script = root / "smoke.ps1"
            script.write_text(copied, encoding="utf-8-sig")
            result = self.powershell("-File", str(script), "-ProjectRoot", str(root),
                                     "-PythonPath", sys.executable)
            self.assertEqual(result.get("status"), "passed", result)
            saved = Path(result["request"]).read_text(encoding="utf-8")
            self.assertNotIn("fixture-unused", saved)
            self.assertNotIn("SUBTITLE_API_KEY", saved)
            from translation_flow.contracts import JobRequest
            video = JobRequest.from_mapping(json.loads(Path(result["video_request"]).read_text(encoding="utf-8")))
            self.assertEqual(video.provider.id, "qwen")
            self.assertEqual(video.provider.endpoint_profile, "qwen-intl")
            self.assertEqual(video.provider.max_output_tokens, 2048)
            self.assertEqual(video.provider.model, "chosen-qwen")
            self.assertTrue(video.additional_mp4)


if __name__ == "__main__":
    unittest.main()
