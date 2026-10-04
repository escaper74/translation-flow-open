from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "Start-TranslationFlow.ps1"
BRIDGE = ROOT / "desktop" / "TranslationFlowProcessBridge.ps1"
SUMMARY = ROOT / "desktop" / "TranslationFlowUiSummary.ps1"
TASK_CENTER = ROOT / "desktop" / "TranslationFlowTaskCenter.ps1"
PROVIDERS = ROOT / "desktop" / "TranslationFlowProvider.ps1"
LAUNCHER = ROOT / "启动翻译流.cmd"
OLD_PREVIEW = ROOT / "预览新版界面.cmd"
CLI = ROOT / "translation_flow" / "cli.py"


class FormalTranslationUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui_text = UI.read_text(encoding="utf-8") + (ROOT / "desktop" / "TranslationFlowView.xaml").read_text(encoding="utf-8")
        cls.bridge_text = BRIDGE.read_text(encoding="utf-8")
        cls.summary_text = SUMMARY.read_text(encoding="utf-8")
        cls.task_center_text = TASK_CENTER.read_text(encoding="utf-8")
        cls.provider_text = PROVIDERS.read_text(encoding="utf-8-sig")
        cls.launcher_text = LAUNCHER.read_text(encoding="utf-8")
        cls.cli_text = CLI.read_text(encoding="utf-8")

    def test_formal_entrypoints_exist_and_launcher_uses_sta(self) -> None:
        self.assertTrue(UI.is_file())
        self.assertTrue(BRIDGE.is_file())
        self.assertTrue(SUMMARY.is_file())
        self.assertTrue(TASK_CENTER.is_file())
        self.assertTrue(LAUNCHER.is_file())
        self.assertIn("-STA", self.launcher_text)
        self.assertIn("desktop\\Start-TranslationFlow.ps1", self.launcher_text)
        self.assertIn("启动翻译流.cmd", OLD_PREVIEW.read_text(encoding="utf-8-sig"))
        self.assertNotIn(
            "Start-TranslationFlowUiV2Prototype.ps1",
            OLD_PREVIEW.read_text(encoding="utf-8-sig"),
        )

    def test_ui_exposes_timing_reuse_and_recovery_summary(self) -> None:
        for fragment in (
            'x:Name="DurationSummaryText"',
            'x:Name="ReuseSummaryText"',
            'x:Name="RecoverySummaryText"',
            'TranslationFlowUiSummary.ps1',
            'function Register-StageStart',
            'function Register-StageEnd',
            'Get-UiReuseItems',
            'Get-UiFailureGuidance',
            '"resumed"',
            '"reused"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)
        self.assertIn("function Format-UiDuration", self.summary_text)
        self.assertIn("function Get-UiFailureGuidance", self.summary_text)

    @unittest.skipUnless(os.name == "nt", "PowerShell summary contract is Windows-only")
    def test_summary_helper_handles_synthetic_events(self) -> None:
        escaped_summary = str(SUMMARY).replace("'", "''")
        command = (
            f". '{escaped_summary}';"
            "$completed=[pscustomobject]@{stages=[pscustomobject]@{"
            "translation=[pscustomobject]@{reused=$true};"
            "picture=[pscustomobject]@{reused=$false};"
            "final_mux=[pscustomobject]@{reused=$true}}};"
            "$observed=@{subtitle_validation=$true};"
            "$items=@(Get-UiReuseItems $completed $observed);"
            "$key=Get-UiFailureGuidance '缺少本次 DeepSeek API Key，且没有可复用的正式翻译结果' 'translation_flow' '翻译主线';"
            "$media=Get-UiFailureGuidance '需要转码或改用 MKV' 'media_repackage' '媒体重新封装';"
            "$lineage=Get-UiFailureGuidance '源片谱系不匹配，当前检查点无法复用' 'translation_flow' '翻译主线';"
            "$event=[pscustomobject]@{timestamp_utc='2026-08-18T00:00:01.5000000+00:00'};"
            "[pscustomobject]@{"
            "durations=@((Format-UiDuration 0.4),(Format-UiDuration 65),(Format-UiDuration 3723));"
            "timestamp=(ConvertTo-UiEventTime $event).ToUniversalTime().ToString('o');"
            "reuse=$items;key=$key;media=$media;lineage=$lineage} | ConvertTo-Json -Depth 7 -Compress"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["durations"], ["<1 秒", "1 分 5 秒", "1 小时 2 分 3 秒"])
        self.assertEqual(payload["timestamp"], "2026-08-18T00:00:01.5000000+00:00")
        reuse = {item["stage"]: item["reused"] for item in payload["reuse"]}
        self.assertEqual(
            reuse,
            {
                "translation": True,
                "picture": False,
                "final_mux": True,
                "subtitle_validation": True,
            },
        )
        self.assertIn("API Key", payload["key"]["summary"])
        self.assertIn("改选 MKV", payload["media"]["next_step"])
        self.assertIn("检查点不一致", payload["lineage"]["summary"])
        self.assertNotIn("API Key", payload["lineage"]["summary"])

    def test_formal_ui_excludes_unsupported_picture_features_and_fake_states(self) -> None:
        for excluded in ("插帧", "演示"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, self.ui_text)
        self.assertNotIn('待接入新版界面', self.ui_text)
        self.assertIn('full_workflow.picture_cli', self.ui_text)

    def test_api_key_is_password_only_and_not_part_of_request(self) -> None:
        self.assertIn('<PasswordBox x:Name="ApiKeyBox"', self.ui_text)
        self.assertNotIn("ApiKeyPlain", self.ui_text)
        self.assertNotIn("ShowApiKey", self.ui_text)

        request_start = self.ui_text.index("$request = [ordered]@{")
        request_end = self.ui_text.index("$json = $request", request_start)
        request_block = self.ui_text[request_start:request_end]
        self.assertNotIn("ApiKey", request_block)
        self.assertNotIn("SUBTITLE_API_KEY", request_block)
        self.assertIn('startInfo.EnvironmentVariables["SUBTITLE_API_KEY"] = apiKey', self.bridge_text)
        self.assertIn('startInfo.EnvironmentVariables.Remove("SUBTITLE_API_KEY")', self.bridge_text)

    def test_api_key_requirement_differs_by_backend_module(self) -> None:
        inputs_start = self.ui_text.index("function Test-RunInputs")
        inputs_end = self.ui_text.index(
            "function Resolve-TranslationPython", inputs_start
        )
        inputs_block = self.ui_text[inputs_start:inputs_end]
        self.assertIn(
            '$runFullWorkflow = (Get-SelectedTaskMode) -eq "translation_picture_final"',
            inputs_block,
        )
        self.assertIn("-and -not $runFullWorkflow", inputs_block)
        self.assertIn("[AllowEmptyString()][string]$ApiKey", self.bridge_text)
        self.assertIn('$BackendModule -eq "translation_flow.cli"', self.bridge_text)

    @unittest.skipUnless(os.name == "nt", "PowerShell bridge contract is Windows-only")
    def test_bridge_allows_empty_key_for_full_workflow_and_media(self) -> None:
        escaped_bridge = str(BRIDGE).replace("'", "''")
        escaped_root = str(ROOT).replace("'", "''")

        def probe(module: str) -> str:
            command = (
                f". '{escaped_bridge}';"
                "try {"
                "Start-TranslationFlowBackend "
                "-PythonPath 'Z:\\codex-does-not-exist\\python.exe' "
                f"-WorkingDirectory '{escaped_root}' "
                f"-RequestPath '{escaped_root}\\missing-request.json' "
                f"-ApiKey '' -BackendModule '{module}' | Out-Null;"
                "'unexpected-success'"
                "} catch { $_.Exception.Message }"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout + result.stderr

        translation_message = probe("translation_flow.cli")
        full_workflow_message = probe("full_workflow.cli")
        media_message = probe("media_planning.cli")
        self.assertIn("DeepSeek API Key 不能为空", translation_message)
        self.assertNotIn("DeepSeek API Key 不能为空", full_workflow_message)
        self.assertNotIn("unexpected-success", full_workflow_message)
        self.assertNotIn("DeepSeek API Key 不能为空", media_message)
        self.assertNotIn("unexpected-success", media_message)

    def test_each_start_has_explicit_current_run_consent_copy(self) -> None:
        self.assertIn("日文字幕、中文翻译草稿及仅由字幕生成的上下文文本", self.ui_text)
        self.assertIn("分批初译、二轮润色和必要时一次假名修复", self.ui_text)
        self.assertIn('DeepSeekModel = "deepseek-v4-pro"', self.ui_text)
        self.assertIn("DeepSeek {1}（效果优先、默认思考模式）", self.ui_text)
        self.assertIn("发送目标仅为 {2}", self.ui_text)
        self.assertIn("按 DeepSeek 账户实际 token 用量计费", self.ui_text)
        self.assertIn('Host = "api.deepseek.com"', self.provider_text)
        self.assertIn("音频、视频和生成的成片始终留在本机", self.ui_text)
        self.assertIn("这项同意只对当前这一次任务有效", self.ui_text)
        self.assertIn('$ConsentOverlay.Visibility = [Windows.Visibility]::Visible', self.ui_text)

    def test_retry_uses_same_job_for_same_video_and_profile(self) -> None:
        self.assertIn("function Get-StableJobId", self.ui_text)
        self.assertIn("$jobId = Get-StableJobId", self.ui_text)
        self.assertIn("$OutputDirectory", self.ui_text)
        self.assertIn("$ProviderModel", self.ui_text)
        self.assertIn("$sourceInfo.Length", self.ui_text)
        self.assertIn("$sourceInfo.LastWriteTimeUtc.Ticks", self.ui_text)
        self.assertNotIn('[Guid]::NewGuid().ToString("D")', self.ui_text)
        self.assertIn("使用同一任务检查点续跑", self.cli_text)
        self.assertNotIn("使用新任务重新开始", self.cli_text)

    def test_request_uses_the_shared_v1_contract(self) -> None:
        for fragment in (
            "schema_version = 1",
            "job_id = $jobId",
            "input_video =",
            "output_dir = $outputDir",
            "profile_id = $profileId",
            "id = $script:ProviderId",
            "host = $script:ProviderHost",
            "model = $script:ProviderModel",
            "current_run = $true",
            "subtitle_text_only = $true",
            "audio_video_local = $true",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)
        self.assertIn('@("normal", "adult_direct")', self.ui_text)

    def test_bridge_launches_cli_and_reads_utf8_jsonl_asynchronously(self) -> None:
        self.assertIn("-m translation_flow.cli --request", self.bridge_text)
        self.assertIn("RedirectStandardOutput = true", self.bridge_text)
        self.assertIn("StandardOutputEncoding = new UTF8Encoding(false)", self.bridge_text)
        self.assertIn("BeginOutputReadLine()", self.bridge_text)
        self.assertIn("ConcurrentQueue<string>", self.bridge_text)
        self.assertIn("ConvertFrom-Json", self.ui_text)
        self.assertIn("Resolve-TranslationPython", self.ui_text)
        self.assertIn("codex-primary-runtime\\dependencies\\python\\python.exe", self.ui_text)

    def test_frontend_hides_internal_routes_and_retains_legacy_contract(self) -> None:
        self.assertIn('x:Name="TaskModeBox" SelectedIndex="0" Visibility="Collapsed"', self.ui_text)
        self.assertIn('Content="精修中文字幕" Tag="translation"', self.ui_text)
        self.assertIn('Text="翻译"', self.ui_text)
        self.assertIn('Text="去马"', self.ui_text)
        self.assertIn('x:Name="SettingsOverlay"', self.ui_text)
        self.assertIn('x:Name="RepackageToolButton"', self.ui_text)
        self.assertNotIn('Text="任务类型"', self.ui_text)
        self.assertIn(
            'Content="字幕与去马" '
            'Tag="translation_picture_final"',
            self.ui_text,
        )
        self.assertNotIn('x:Name="ContinueFullWorkflowCheckBox"', self.ui_text)
        self.assertIn('"full_workflow.cli"', self.ui_text)
        self.assertIn('"full_workflow.cli"', self.bridge_text)
        self.assertIn('workflow_mode"] = "translation_picture_final"', self.ui_text)
        self.assertIn("picture_and_mux_30m_human_accepted = $true", self.ui_text)
        self.assertIn("只有 Whisper A/C 已结束才启动 GPU", self.ui_text)
        self.assertIn("30 分钟路线", self.ui_text)

    def test_formal_ui_has_a_separate_keyless_media_repackage_mode(self) -> None:
        for fragment in (
            'x:Name="TaskModeBox"',
            'Tag="media_repackage"',
            'Tag="mkv"',
            'Tag="mp4"',
            'workflow_mode = "media_repackage"',
            'source_media =',
            'output_target = $outputTarget',
            '"media_planning.cli"',
            'Get-StableMediaJobId',
            'Get-JsonProperty $outputs "final_media"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)
        self.assertIn('"media_planning.cli"', self.bridge_text)
        controls = ET.parse(ROOT / 'desktop' / 'TranslationFlowView.xaml').iter()
        output_format = next(node for node in controls if node.get('{http://schemas.microsoft.com/winfx/2006/xaml}Name') == 'OutputFormatBox')
        self.assertEqual('0', output_format.get('SelectedIndex'))
        media_start = self.ui_text.index('$recordRoot = ".media-repackage"')
        media_request_start = self.ui_text.index("$request = [ordered]@{", media_start)
        media_request_end = self.ui_text.index(
            '$backendModule = "media_planning.cli"', media_request_start
        )
        media_request = self.ui_text[media_request_start:media_request_end]
        for forbidden in ("provider", "consent", "profile_id", "ApiKey"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, media_request)
        self.assertIn(
            'if ((Get-SelectedTaskMode) -eq "media_repackage")', self.ui_text
        )

    def test_full_workflow_can_request_an_isolated_additional_mp4(self) -> None:
        for fragment in (
            'additional_mp4 = (Get-SelectedOutputTarget) -eq "mp4"',
            "按所选格式保存字幕与去马成品",
            "追加失败不会推翻或删除 MKV",
            'Get-JsonProperty $outputs "final_mp4"',
            'Get-JsonProperty $outputs "mp4_technical_report"',
            'Get-JsonProperty $mp4Stage "status"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)

    def test_formal_ui_exposes_local_task_center_and_safe_resume(self) -> None:
        for fragment in (
            "TranslationFlowTaskCenter.ps1",
            'x:Name="TaskHistoryGrid"',
            'x:Name="ResumeTaskButton"',
            "Get-TaskCenterEntries",
            "Get-TaskCenterPreferredEntry",
            "Get-TaskCenterResumeRequest",
            "按原请求继续",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)
        self.assertIn("request.json", self.task_center_text)
        self.assertIn("events.jsonl", self.task_center_text)
        self.assertIn("workflow.result.json", self.task_center_text)
        self.assertNotIn("Start-Process", self.task_center_text)

    def test_running_task_center_refreshes_from_authoritative_events(self) -> None:
        for fragment in (
            '$entry.StatusLabel = "运行中"',
            "$entry.CanResume = $false",
            "$RefreshTaskCenterButton.IsEnabled = $true",
            '"job_started",',
            '"stage_started",',
            '"stage_completed",',
            '"stage_failed",',
            '"job_completed",',
            '"job_failed"',
            "Refresh-TaskCenter",
            "Complete-ExitedRunForTaskCenter",
            "$script:TaskCenterCompletingExitedRun",
            "$TaskHistoryGrid.SelectedItem = $selection",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)

    @unittest.skipUnless(os.name == "nt", "PowerShell task center contract is Windows-only")
    def test_task_center_reads_synthetic_failure_and_reconstructs_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "deliverables"
            job = output / ".full-workflow" / "job-task-center"
            job.mkdir(parents=True)
            request = {
                "schema_version": 1,
                "workflow_mode": "translation_picture_final",
                "job_id": "job-task-center",
                "input_video": str(source),
                "output_dir": str(output),
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
                "delivery": {"additional_mp4": True},
            }
            (job / "request.json").write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            (job / "workflow.json").write_text(
                json.dumps({"status": "failed", "error": "synthetic failure"}),
                encoding="utf-8",
            )
            (job / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "job_failed",
                        "stage": "picture_workflow",
                        "detail": "synthetic failure",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            helper = str(TASK_CENTER).replace("'", "''")
            output_arg = str(output).replace("'", "''")
            request_arg = str(job / "request.json").replace("'", "''")
            command = (
                f". '{helper}';"
                f"$entry=@(Get-TaskCenterEntries '{output_arg}')[0];"
                f"$preferred=Get-TaskCenterPreferredEntry @(Get-TaskCenterEntries '{output_arg}') '';"
                f"$resume=Get-TaskCenterResumeRequest '{request_arg}';"
                "[pscustomobject]@{entry=$entry;preferred=$preferred;resume=$resume}|ConvertTo-Json -Depth 7 -Compress"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["entry"]["StatusLabel"], "失败 / 可恢复")
            self.assertEqual(payload["entry"]["FailureText"], "synthetic failure")
            self.assertTrue(payload["entry"]["CanResume"])
            self.assertEqual(payload["entry"]["RequestPath"], payload["preferred"]["RequestPath"])
            self.assertTrue(payload["resume"]["FullWorkflow"])
            self.assertEqual(payload["resume"]["OutputTarget"], "mp4")
            self.assertEqual(payload["resume"]["ProfileId"], "adult_direct")

    @unittest.skipUnless(os.name == "nt", "PowerShell task center contract is Windows-only")
    def test_task_center_rejects_request_that_contains_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            request_path = Path(raw) / "request.json"
            request_path.write_text(
                json.dumps({"schema_version": 1, "api_key": "must-not-load"}),
                encoding="utf-8",
            )
            helper = str(TASK_CENTER).replace("'", "''")
            request_arg = str(request_path).replace("'", "''")
            command = (
                f". '{helper}';"
                f"try {{ Get-TaskCenterResumeRequest '{request_arg}' | Out-Null; 'unexpected' }} "
                "catch { $_.Exception.Message }"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("API Key", result.stdout)
            self.assertNotIn("unexpected", result.stdout)

    def test_ui_does_not_use_filename_extension_as_the_final_input_gate(self) -> None:
        selected_start = self.ui_text.index("function Set-SelectedVideo")
        selected_end = self.ui_text.index("function Test-RunInputs", selected_start)
        selected_block = self.ui_text[selected_start:selected_end]
        # SRT extension selects the text-only mode; video admission stays probe-based.
        self.assertIn('Sync-FeatureMode', selected_block)
        self.assertNotIn('".mkv"', selected_block)
        self.assertNotIn("$allowed", selected_block)
        self.assertIn("所有文件|*.*", self.ui_text)

    def test_windows_argument_quoting_does_not_double_every_backslash(self) -> None:
        self.assertIn("CommandLineToArgvW-compatible quoting", self.bridge_text)
        self.assertIn("backslashes * 2", self.bridge_text)
        self.assertNotIn('value.Replace("\\\\", "\\\\\\\\")', self.bridge_text)

    def test_success_gate_rejects_false_success(self) -> None:
        for fragment in (
            '"job_completed"',
            "$exitCode -ne 0",
            '$technicalStatus -ne "passed"',
            "[IO.File]::Exists($item)",
            'Get-JsonProperty $outputs "chinese_srt"',
            'Get-JsonProperty $outputs "final_mkv"',
            'Get-JsonProperty $outputs "technical_report"',
            'Get-JsonProperty $technical "report_path"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.ui_text)

    @unittest.skipUnless(os.name == "nt", "PowerShell/WPF contract is Windows-only")
    def test_powershell_files_parse(self) -> None:
        for path in (UI, BRIDGE, SUMMARY, TASK_CENTER, PROVIDERS):
            escaped = str(path).replace("'", "''")
            command = (
                "$tokens=$null;$errors=$null;"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
                "[ref]$tokens,[ref]$errors);"
                "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell/WPF contract is Windows-only")
    def test_validate_only_loads_without_showing_a_window(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-STA",
                "-File",
                str(UI),
                "-ValidateOnly",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(payload["protocol_schema"], 1)
        self.assertEqual(payload["ui_summary_contract"], 1)
        self.assertEqual(payload["task_center_contract"], 1)
        self.assertEqual(payload["mp4_delivery_contract"], 1)
        self.assertEqual(payload["default_provider"], "codex")
        self.assertFalse(payload["codex_original_picture_only"])

    def test_codex_controls_and_cancel_use_official_status_and_local_signal(self) -> None:
        for text in (
            'x:Name="ProviderBox" SelectedIndex="0"',
            'Tag="codex"',
            'Tag="deepseek"',
            'x:Name="CodexStatusText"',
            'x:Name="ApiKeyPanel" Visibility="Collapsed"',
            "Start-CodexStatusCheck",
            "StartCodexStatus",
            'Sync-FeatureMode',
            "Request-TranslationFlowCancellation $script:CurrentRequestPath",
            '"job_cancelled"',
            "$exitCode -eq 2",
            "正在保存当前进度",
        ):
            self.assertIn(text, self.ui_text, text)
        self.assertIn("-m translation_flow.cli --codex-status", self.bridge_text)
        self.assertNotIn("WaitForExit()", self.ui_text)
        self.assertNotIn("taskkill", self.ui_text.lower())
        self.assertNotIn("auth.json", self.ui_text + self.bridge_text)
        start_block = self.ui_text.split("function Start-ConfirmedRun", 1)[1].split("function Begin-ConfiguredRun", 1)[0]
        self.assertIn("$StopRunButton.IsEnabled = $false", start_block)
        self.assertNotIn("$StopRunButton.IsEnabled = $script:CurrentProviderId", start_block)

    @staticmethod
    def _powershell(command: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + command],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
        return result

    @unittest.skipUnless(os.name == "nt", "PowerShell contract is Windows-only")
    def test_codex_bridge_accepts_empty_key_but_rejects_full_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            request = Path(raw) / "request.json"
            request.write_text(json.dumps({"provider": {"id": "codex"}}), encoding="utf-8")
            command = (
                f". '{str(BRIDGE).replace(chr(39), chr(39) * 2)}';"
                "$results=@();foreach($module in @('translation_flow.cli','full_workflow.cli')){"
                "try{Start-TranslationFlowBackend -PythonPath 'Z:\\missing\\python.exe' "
                f"-WorkingDirectory '{str(ROOT)}' -RequestPath '{request}' "
                "-ApiKey '' -BackendModule $module|Out-Null;$results+='unexpected'}"
                "catch{$results+=$_.Exception.Message}};$results|ConvertTo-Json -Compress"
            )
            result = self._powershell(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            messages = json.loads(result.stdout)
            self.assertNotIn("DeepSeek API Key", messages[0])
            self.assertNotIn("unexpected", messages[0])
            self.assertNotIn("不能启动完整画面流程", messages[1])
            self.assertNotIn("unexpected", messages[1])

    @unittest.skipUnless(os.name == "nt", "PowerShell contract is Windows-only")
    def test_provider_restore_preserves_legacy_model_and_codex_identity(self) -> None:
        command = (
            f". '{str(PROVIDERS)}';"
            "@((Get-TranslationProviderSettings 'deepseek' 'deepseek-v4-flash'),"
            "(Get-TranslationProviderSettings 'codex' 'cli-default'))|ConvertTo-Json -Compress"
        )
        result = self._powershell(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values[0], {"Id": "deepseek", "Host": "api.deepseek.com", "Model": "deepseek-v4-flash"})
        self.assertEqual(values[1], {"Id": "codex", "Host": "chatgpt.com", "Model": "cli-default"})

    @unittest.skipUnless(os.name == "nt", "PowerShell contract is Windows-only")
    def test_codex_cancelled_task_restores_provider_mp4_and_signal_safely(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mkv"
            source.write_bytes(b"fixture")
            job = root / ".translation-flow" / "job-codex"
            job.mkdir(parents=True)
            request = job / "request.json"
            payload = {
                "schema_version": 1, "job_id": "job-codex", "input_video": str(source),
                "output_dir": str(root), "profile_id": "normal",
                "provider": {"id": "codex", "host": "chatgpt.com", "model": "cli-default"},
                "delivery": {"additional_mp4": True},
            }
            request.write_text(json.dumps(payload), encoding="utf-8")
            (job / "events.jsonl").write_text(json.dumps({"event": "job_cancelled", "message": "Codex stopped"}) + "\n", encoding="utf-8")
            before = request.read_bytes()
            result = self._powershell(
                f". '{TASK_CENTER}';$entry=@(Get-TaskCenterEntries '{root}')[0];"
                f"$resume=Get-TaskCenterResumeRequest '{request}';"
                f"$signal=Request-TranslationFlowCancellation '{request}';"
                "[pscustomobject]@{entry=$entry;resume=$resume;signal=$signal}|ConvertTo-Json -Depth 7 -Compress"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["entry"]["StatusLabel"], "已停止 / 可恢复")
            self.assertIn("Codex", data["entry"]["ProviderLabel"])
            self.assertEqual(data["resume"]["ProviderId"], "codex")
            self.assertEqual(data["resume"]["ProviderModel"], "cli-default")
            self.assertEqual(data["resume"]["OutputTarget"], "mp4")
            self.assertEqual(Path(data["signal"]), job / "cancel.requested")
            self.assertTrue((job / "cancel.requested").is_file())
            self.assertEqual(request.read_bytes(), before)
            payload["workflow_mode"] = "translation_picture_final"
            request.write_text(json.dumps(payload), encoding="utf-8")
            result = self._powershell(
                f". '{TASK_CENTER}';try{{(Get-TaskCenterResumeRequest '{request}').TaskMode}}"
                "catch{$_.Exception.Message}"
            )
            self.assertIn("translation_picture_final", result.stdout)
            self.assertNotIn("unexpected", result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell contract is Windows-only")
    def test_codex_failure_guidance_does_not_request_api_key(self) -> None:
        result = self._powershell(
            f". '{SUMMARY}';@('Codex 登录失效','Codex 额度不足','Codex 任务取消')|"
            "ForEach-Object{Get-UiFailureGuidance $_ 'translate_initial' '初译'}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertIn("codex login", values[0]["next_step"])
        self.assertIn("等待账户额度恢复", values[1]["next_step"])
        self.assertIn("只补未完成部分", values[2]["next_step"])
        self.assertNotIn("填写本次 Key", result.stdout)

    @unittest.skipUnless(os.name == "nt", "WPF interaction is Windows-only")
    def test_wpf_provider_mode_events_and_async_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "translation_flow"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "cli.py").write_text(
                "import json,time\ntime.sleep(0.2)\nprint(json.dumps({'status':'available','message':'fixture','cli_version':'fixture'}))\n",
                encoding="utf-8",
            )
            source = root / "source.mkv"
            source.write_bytes(b"local fixture; never processed")
            job = root / ".translation-flow" / "job-offline-codex"
            job.mkdir(parents=True)
            (job / "request.json").write_text(json.dumps({
                "schema_version": 1, "job_id": job.name, "input_video": str(source),
                "output_dir": str(root), "profile_id": "normal",
                "provider": {"id": "codex", "host": "chatgpt.com", "model": "cli-default"},
                "consent": {"current_run": True, "subtitle_text_only": True, "audio_video_local": True},
            }), encoding="utf-8")
            (job / "job.result.json").write_text(json.dumps({
                "technical_validation": {"status": "passed"},
            }), encoding="utf-8")
            injection = r'''
$script:UiTestResult = $null
$script:UiTestStarted = [DateTime]::UtcNow
$script:UiTestTimer = New-Object System.Windows.Threading.DispatcherTimer
$script:UiTestTimer.Interval = [TimeSpan]::FromMilliseconds(100)
$script:UiTestTimer.Add_Tick({
    try {
        if (([DateTime]::UtcNow - $script:UiTestStarted).TotalSeconds -gt 10) { throw 'status timeout' }
        if ($script:CodexStatus -eq 'checking') { return }
        if ($script:CodexStatus -ne 'available') { throw ('status unavailable: ' + $CodexStatusText.Text + ' output=' + ($script:CodexStatusLines -join ' ')) }
        if ($script:ProviderId -ne 'codex' -or (Get-SelectedTaskMode) -ne 'translation') { throw 'wrong default' }
        if ($WhisperModelBox.SelectedItem.Tag -ne 'large-v3-turbo' -or $script:CurrentWhisperAModel -ne 'large-v3-turbo') { throw 'Turbo is not the desktop default' }
        if ([string]$WhisperModelPanel.Visibility -ne 'Visible') { throw 'ASR model selector hidden' }
        # The empty input fails validation before any backend; exercise the real click binding.
        Select-ComboBoxItemByTag $WhisperModelBox 'large-v3'
        $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
        if ($script:CurrentWhisperAModel -ne 'large-v3') { throw 'start ignored large-v3 choice' }
        Select-ComboBoxItemByTag $WhisperModelBox 'large-v3-turbo'
        $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
        if ($script:CurrentWhisperAModel -ne 'large-v3-turbo') { throw 'start ignored Turbo choice' }
        $InputErrorText.Text=''
        Select-ComboBoxItemByTag $TaskModeBox 'srt_translation'
        if ([string]$WhisperModelPanel.Visibility -ne 'Collapsed') { throw 'SRT should not offer ASR' }
        Select-ComboBoxItemByTag $TaskModeBox 'translation'
        if ([string]$ApiKeyPanel.Visibility -ne 'Collapsed') { throw 'Key visible' }
        Select-ComboBoxItemByTag $OutputFormatBox 'mp4'
        Select-ComboBoxItemByTag $ProviderBox 'deepseek'
        if (-not $OutputFormatBox.IsEnabled -or (Get-SelectedOutputTarget) -ne 'mp4') { throw 'DeepSeek MP4 choice lost' }
        if ($OutputFormatHintText.Text -notmatch '格式') { throw 'missing delivery hint' }
        Select-ComboBoxItemByTag $TaskModeBox 'translation_picture_final'
        if ([string]$WhisperModelPanel.Visibility -ne 'Visible') { throw 'combined mode lost ASR selector' }
        if ($script:ProviderId -ne 'deepseek') { throw 'full workflow provider mismatch' }
        if (-not $OutputFormatBox.IsEnabled) { throw 'full workflow MP4 disabled' }
        Select-ComboBoxItemByTag $TaskModeBox 'media_repackage'
        if ([string]$WhisperModelPanel.Visibility -ne 'Collapsed') { throw 'repackage should not offer ASR' }
        if (-not $OutputFormatBox.IsEnabled) { throw 'media MP4 disabled' }
        Select-ComboBoxItemByTag $TaskModeBox 'translation_picture_final'
        $ApiKeyBox.Password = 'synthetic-unused'
        Select-ComboBoxItemByTag $ProviderBox 'codex'
        if ((Get-SelectedTaskMode) -ne 'translation_picture_final' -or $ApiKeyBox.Password) { throw 'Codex mode or Key mismatch' }
        if (-not $OutputFormatBox.IsEnabled) { throw 'MP4 option disabled' }
        $script:CurrentProviderId = 'codex'
        $script:CurrentWorkflowMode = 'translation'
        $script:CurrentJobId = 'job-ui-start-signal'
        if ($StopRunButton.IsEnabled) { throw 'Stop enabled before job_started' }
        Handle-BackendEvent ([pscustomobject]@{schema_version=1;job_id=$script:CurrentJobId;seq=1;event='job_started'})
        if (-not $StopRunButton.IsEnabled) { throw 'Stop disabled after Codex job_started' }
        $OutputDirectoryBox.Text = $ProjectRoot
        Refresh-TaskCenter
        $TaskHistoryGrid.SelectedItem = $TaskHistoryGrid.Items[0]
        $script:CodexStatus = 'unavailable'
        $ResumeTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        if ([string]$ConsentOverlay.Visibility -ne 'Visible' -or $InputErrorText.Text) { throw 'offline completed Codex restore blocked by UI' }
        if ($WhisperModelBox.SelectedItem.Tag -ne 'large-v3' -or $script:CurrentWhisperAModel -ne 'large-v3') { throw 'legacy resume switched to Turbo' }
        $script:UiEnteredBackend = $false
        function script:Start-ConfirmedRun { $script:UiEnteredBackend = $true }
        $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        if (-not $script:UiEnteredBackend) { throw 'offline restore cannot enter backend after consent' }
        $script:IsRunning=$false
        $NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
        if ($WhisperModelBox.SelectedItem.Tag -ne 'large-v3-turbo' -or $script:CurrentWhisperAModel -ne 'large-v3-turbo') { throw 'new task did not reset to default model' }
        $script:UiTestResult = @{status='passed';provider=$script:ProviderId;mode=(Get-SelectedTaskMode)}
    }
    catch { $script:UiTestResult = @{status='failed';error=$_.Exception.Message} }
    if ($null -ne $script:UiTestResult) { $script:UiTestTimer.Stop();$Window.Close() }
})
$script:UiTestTimer.Start()
[void]$Window.ShowDialog()
$script:UiTestResult | ConvertTo-Json -Compress
'''
            copied = UI.read_text(encoding="utf-8-sig").replace("$PSScriptRoot", "'" + str(UI.parent).replace("'", "''") + "'")
            copied = copied.replace("[void]$Window.ShowDialog()", injection)
            smoke = root / "smoke.ps1"
            smoke.write_text(copied.lstrip("\ufeff"), encoding="utf-8-sig")
            result = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
                 "-File", str(smoke), "-ProjectRoot", str(root), "-PythonPath", sys.executable],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                env={name.upper(): value for name, value in os.environ.items()},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(value.get("status"), "passed", value)


if __name__ == "__main__":
    unittest.main()
