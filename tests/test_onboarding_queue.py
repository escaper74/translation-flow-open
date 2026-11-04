"""Regression coverage for first use and composing while the queue runs."""
import os
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from translation_flow.desktop_access import check_readiness
from translation_flow.diagnostics import inspect_environment
from tests import test_frontend_presentation as frontend


class OnboardingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "WPF is Windows-only")
    def test_default_output_is_independent_of_source_checkout_and_override_is_kept(self):
        import subprocess
        from translation_flow.translation.pipeline import validate_cache_path
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for output in (None, root / "chosen-output"):
                command = "& '" + str(frontend.UI).replace("'", "''") + "' -ValidateOnly -ProjectRoot '" + str(root) + "'"
                if output:
                    command += " -OutputDirectory '" + str(output) + "'"
                run = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-Command", command],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
                self.assertEqual(0, run.returncode, run.stderr[-2000:])
                result = json.loads(run.stdout.strip().splitlines()[-1])
                actual = Path(result["output_directory"])
                if output:
                    self.assertEqual(output, actual)
                else:
                    self.assertEqual("TranslationFlow", actual.name)
                    self.assertNotEqual(root, actual.parent)
                validate_cache_path(actual / ".translation-flow" / ("job-" + "0" * 32) / "translations")

    def test_first_picture_use_requires_components_not_history(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True), patch(
            "subprocess.run", side_effect=AssertionError("path inventory only")
        ):
            root = Path(raw)
            for mode in ("picture", "full"):
                report = inspect_environment(mode=mode, provider="deepseek", tools=root)
                for item in report["checks"]:
                    if item["present"] or item["name"] == "acceptance_report":
                        continue
                    path = Path(item["path"])
                    if item["name"] == "faster_model":
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"non-executable path fixture")
                report = inspect_environment(mode=mode, provider="deepseek", tools=root)
                self.assertEqual("paths_available", report["status"], report)
                self.assertNotIn("acceptance_report", [x["name"] for x in report["checks"]])
            result = check_readiness(mode="picture", provider="codex", tools=root, probe=False)
            self.assertEqual("ready", result["status"])
            self.assertFalse(result["service_tested"])

    def test_missing_engine_is_named_and_translation_modes_do_not_require_it(self):
        from jasna_picture.workflow import required_engine_paths
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            root = Path(raw)
            report = inspect_environment(mode="picture", tools=root)
            for item in report["checks"]:
                path = Path(item["path"])
                if not item["present"]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"path fixture")
            missing = required_engine_paths(root / "jasna/jasna.exe")[0]
            missing.unlink()
            result = check_readiness(mode="picture", provider="codex", tools=root)
            self.assertEqual("blocked", result["status"])
            self.assertIn(missing.name, result["message"])
            for mode in ("srt", "video", "repackage"):
                self.assertFalse(any(x["name"].startswith("TensorRT") for x in inspect_environment(mode=mode, provider="deepseek", tools=root)["checks"]))

    def test_new_task_runtime_overrides_already_running_worker_environment(self):
        from translation_flow.task_queue import TaskQueue
        from tests.test_task_queue import make_request
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"TRANSLATION_FLOW_JASNA": "old-jasna.exe"}):
            root = Path(raw)
            queue = TaskQueue(root / "state")
            request = make_request(root, "configured")
            runtime = {"python_path": sys.executable, "tool_root": str(root / "new-tools"), "jasna_path": str(root / "new-jasna.exe")}
            row = queue.command({"action": "enqueue", "authorized": True, "items": [{"request_path": str(request), **runtime}]})["items"][0]
            with patch("translation_flow.task_queue.subprocess.Popen") as launch:
                queue._launch(row, "")
            args, kwargs = launch.call_args
            self.assertEqual(runtime["python_path"], args[0][0])
            self.assertEqual(runtime["tool_root"], args[0][-1])
            self.assertEqual(runtime["jasna_path"], kwargs["env"]["TRANSLATION_FLOW_JASNA"])
            persisted = json.loads((root / "state/queue.json").read_text(encoding="utf-8"))["items"][0]
            for key, value in runtime.items():
                self.assertEqual(value, persisted[key])



@unittest.skipUnless(os.name == "nt", "WPF is Windows-only")
class QueueDraftTests(unittest.TestCase):
    run_ui = frontend.FrontendPresentationTests.run_ui
    isolated_queue = frontend.FrontendPresentationTests.isolated_queue


    def test_same_name_recovery_cards_show_location_and_keep_selected_request(self):
        from tests.test_task_queue import make_request
        from translation_flow.task_queue import request_info
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            records = []
            originals = {}
            for directory in ("old", "out"):
                output = root / directory
                output.mkdir()
                request = make_request(output, "same-name")
                checkpoint = request.parent / "part-0.fixture"
                checkpoint.write_bytes(b"already completed")
                originals[request] = request.read_bytes()
                originals[checkpoint] = checkpoint.read_bytes()
                records.append(dict(request_info(request), status="failed", stage="translate_context",
                                    progress=1 if directory == "old" else 28.8, message="fixture failure"))
            (root / "rows.json").write_text(json.dumps(records), encoding="utf-8")
            self.run_ui(root, r"""
function script:Start-QueueCall($Payload) { throw 'selection must not submit or resume work' }
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$items=Get-Content -LiteralPath 'TESTROOT\rows.json' -Raw | ConvertFrom-Json
$queue=[pscustomobject]@{project_root=$ProjectRoot;items=$items;paused=$false}
Apply-QueueSnapshot ([pscustomobject]@{Queue=$queue;Watched=$null;Events=@()})
$cards=(Get-Control 'RecoverTasksHost').Children
if ($cards.Count -ne 2 -or $cards[0].FindName('Title').Text -ne $cards[1].FindName('Title').Text) { throw 'same-name fixture lost' }
if ($cards[0].FindName('OutputLocation').Text -ne '保存到：TESTROOT\old' -or $cards[1].FindName('OutputLocation').Text -ne '保存到：TESTROOT\out') { throw 'recovery cards cannot be distinguished by location' }
$QueueGrid.SelectedIndex=0
$cards[1].FindName('Buttons').Children[0].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($script:PendingQueueResumeId -ne $items[1].id -or $script:PendingSrtRequestPath -ne $items[1].request_path -or $OutputDirectoryBox.Text -ne 'TESTROOT\out') { throw ('wrong request selected: '+$InputErrorText.Text) }
$entry=@($TaskHistoryGrid.ItemsSource | Where-Object { $_.RequestPath -eq $items[1].request_path })[0]
$historyCard=New-DesignTaskCard $null 'recover' $entry
if ($historyCard.FindName('OutputLocation').ToolTip -ne 'TESTROOT\out') { throw 'history card lost output location' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)))
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    def test_failure_reason_survives_queue_hint_and_clears_on_next_run(self):
        from tests.test_task_queue import make_request
        from translation_flow.task_queue import request_info
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = make_request(root, "failure")
            record = dict(request_info(request), status="failed", stage="translation_flow", progress=1,
                          message="failed", detail="输出目录过深，Windows 无法写入字幕批次缓存；请选择较短的保存位置。",
                          recovery_hint="保留阶段报告与产物；修正原因后继续。", started_at=root.stat().st_mtime)
            (root / "row.json").write_text(json.dumps(record), encoding="utf-8")
            self.run_ui(root, r"""
$OutputDirectoryBox.Text='TESTROOT'
Set-UiPage 'run'
$item=Get-Content -LiteralPath 'TESTROOT\row.json' -Raw | ConvertFrom-Json
$queue=[pscustomobject]@{project_root=$ProjectRoot;items=@($item);paused=$false}
Apply-QueueSnapshot ([pscustomobject]@{Queue=$queue;Watched=$item;Events=@()})
$detail=Get-Control 'FailureDetailText'
$Window.UpdateLayout()
if ($detail.Text -ne $item.detail -or -not $detail.IsVisible -or $detail.ActualHeight -le 0) { throw 'actual failure reason is hidden' }
if ($RecoverySummaryText.Text -ne $item.recovery_hint) { throw 'recovery guidance was lost' }
Reset-RunPresentation
if ($detail.Text -or [string]$detail.Visibility -ne 'Collapsed') { throw 'old failure leaked into another run' }
Set-FailureSummary 'connection timeout'
Complete-RunPresentation $false $true
if ([string]$detail.Visibility -ne 'Collapsed') { throw 'stopped task retained unrelated error' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)))

    def test_resume_ui_sends_current_runtime_without_new_service_probe(self):
        from tests.test_task_queue import make_request
        from translation_flow.queue_cli import rpc
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = make_request(root, "resume")
            checkpoint = request.parent / "part-0.fixture"
            checkpoint.write_bytes(b"already completed")
            original_request = request.read_bytes()
            with self.isolated_queue(root) as (project, state, _, worker):
                rpc(state, {"action": "pause"})
                rpc(state, {"action": "enqueue", "authorized": True, "items": [{
                    "request_path": str(request), "python_path": "old-python.exe",
                    "tool_root": "old-tools", "jasna_path": "old-jasna.exe"}]})
                self.run_ui(root, r"""
if ($null -eq $script:QueueSnapshot -or $script:QueueSnapshot.items.Count -ne 1) { return }
if ($script:TestStep -eq 0) {
    $ToolRootBox.Text='TESTROOT\corrected-tools'
    $JasnaPathBox.Text='TESTROOT\corrected-jasna.exe'
    $QueueGrid.SelectedIndex=0
    $ResumeQueueItemButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if (-not $script:PendingQueueResumeId) { throw ('resume was not loaded: '+$InputErrorText.Text) }
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1; return
}
if ($null -ne $script:AccessBridge) { throw 'resume started a new service probe' }
if ($InputErrorText.Text) { throw $InputErrorText.Text }
$row=$script:QueueSnapshot.items[0]
if ($row.status -ne 'completed') { return }
if ($row.python_path -ne $PythonPathBox.Text -or $row.tool_root -ne 'TESTROOT\corrected-tools' -or $row.jasna_path -ne 'TESTROOT\corrected-jasna.exe') { throw 'resume kept outdated runtime settings' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)), state=state, project=project, mock_readiness=False)
            self.assertEqual(original_request, request.read_bytes())
            self.assertEqual(b"already completed", checkpoint.read_bytes())

    def test_running_task_allows_new_draft_and_serial_submission(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("first", "second"):
                (root / f"{name}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            with self.isolated_queue(root) as (project, state, _, worker):
                self.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\first.srt')
    $OutputDirectoryBox.Text='TESTROOT'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1; return
}
if ($script:TestStep -eq 1) {
    if ($script:QueueSnapshot.items.Count -ne 1 -or $script:QueueSnapshot.items[0].status -ne 'running') { return }
    $NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    # Exercise the real home route without opening a file picker.
    $BrowseVideoButton=New-Object Windows.Controls.Button
    $BrowseVideoButton.Add_Click({Set-SelectedFiles @('TESTROOT\second.srt')})
    (Get-Control 'HomeTranslationButton').RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($script:UiPage -ne 'setup' -or $InputVideoBox.Text -ne 'TESTROOT\second.srt' -or -not $StartButton.IsEnabled) { throw 'running task blocked a new draft' }
    [void](Start-QueueCall @{action='status'})
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    # An already queued WPF click must not generate another submission.
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=2; return
}
$items=@($script:QueueSnapshot.items)
if ($items.Count -gt 2) { throw 'duplicate submission' }
if ($items.Count -ne 2 -or $items[1].status -ne 'completed') { return }
if ($items[0].status -ne 'completed' -or $items[0].finished_at -gt $items[1].started_at) { throw 'queue lost serial execution' }
$NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($InputVideoBox.Text -or -not $BrowseVideoButton.IsEnabled) { throw 'completed task blocked New' }
$script:TestResult=@{status='passed'}
'''.replace("TESTROOT", str(root)), state=state, project=project)

    def test_running_task_terminal_states_preserve_draft(self):
        for terminal in ("completed", "failed", "stopped"):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                for name in ("first", "draft"):
                    (root / f"{name}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
                with self.isolated_queue(root) as (project, state, _, worker):
                    self.run_ui(root, r"""
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\first.srt'); $OutputDirectoryBox.Text='TESTROOT'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1; return
}
if ($script:TestStep -eq 1) {
    if ($script:QueueSnapshot.items.Count -ne 1 -or $script:QueueSnapshot.items[0].status -ne 'running') { return }
    $NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $BrowseVideoButton=New-Object Windows.Controls.Button
    $BrowseVideoButton.Add_Click({Set-SelectedFiles @('TESTROOT\draft.srt')})
    (Get-Control 'HomeTranslationButton').RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $OutputDirectoryBox.Text='TESTROOT\draft-output'
    $ToolRootBox.Text='TESTROOT\chosen-tools'
    $JasnaPathBox.Text='TESTROOT\chosen-jasna.exe'
    $script:DraftItem=$script:QueueSnapshot.items[0]
    if ('TERMINAL' -eq 'failed') { [IO.File]::WriteAllText((Join-Path ([IO.Path]::GetDirectoryName($script:DraftItem.request_path)) 'fail.fixture'), 'fixture') }
    if ('TERMINAL' -eq 'stopped') { [void](Start-QueueCall @{action='stop'; id=$script:DraftItem.id}) }
    $script:TestStep=2; return
}
if ($InputVideoBox.Text -ne 'TESTROOT\draft.srt' -or $script:SelectedFiles.Count -ne 1 -or $script:SelectedFiles[0] -ne 'TESTROOT\draft.srt' -or $OutputDirectoryBox.Text -ne 'TESTROOT\draft-output' -or $ToolRootBox.Text -ne 'TESTROOT\chosen-tools' -or $JasnaPathBox.Text -ne 'TESTROOT\chosen-jasna.exe' -or -not $StartButton.IsEnabled) { throw 'poll replaced the draft' }
if ($script:QueueSnapshot.items[0].status -ne 'TERMINAL') { return }
if ($script:UiPage -ne 'setup' -or $InputVideoBox.Text -ne 'TESTROOT\draft.srt' -or -not $StartButton.IsEnabled) { throw 'terminal response destroyed draft' }
if ($QueueGrid.ItemsSource[0].Item.status -ne 'TERMINAL') { throw 'draft froze queue cards' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)).replace("TERMINAL", terminal), state=state, project=project)

    def test_rejected_submission_keeps_batch_during_running_job_refresh(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("first", "second", "third"):
                (root / f"{name}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            with self.isolated_queue(root) as (project, state, _, worker):
                self.run_ui(root, r"""
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\first.srt'); $OutputDirectoryBox.Text='TESTROOT'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1; return
}
if ($script:TestStep -eq 1) {
    if ($script:QueueSnapshot.items.Count -ne 1 -or $script:QueueSnapshot.items[0].status -ne 'running') { return }
    Set-SelectedFiles @('TESTROOT\second.srt','TESTROOT\third.srt')
    $ToolRootBox.Text='TESTROOT\tools'; $JasnaPathBox.Text='TESTROOT\jasna.exe'
    [void](Start-QueueCall @{action='status'})
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($null -eq $script:PendingQueuePayload) { throw 'fixture did not race a status RPC' }
    foreach ($item in $script:PendingQueuePayload.items) { $item.request_path='TESTROOT\missing.json' }
    $script:TestStep=2; return
}
if ($script:TestStep -eq 2) {
    if (-not $InputErrorText.Text) { return }
    if ($script:QueueSubmitting) { return }
    $script:QueuePollAt=[DateTime]::MinValue
    $script:TestStep=3; return
}
if ($null -ne $script:QueueCall) { return }
if ($script:SelectedFiles.Count -ne 2 -or $InputVideoBox.Text -ne 'TESTROOT\second.srt' -or $ToolRootBox.Text -ne 'TESTROOT\tools' -or $JasnaPathBox.Text -ne 'TESTROOT\jasna.exe' -or -not $StartButton.IsEnabled -or $script:UiPage -ne 'setup') { throw 'rejected draft lost files or settings after refresh' }
if ($script:QueueSnapshot.items.Count -ne 1) { throw 'rejected batch was accepted' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)), state=state, project=project, allow_queue_errors=True)

    def test_first_picture_settings_use_actual_readiness_and_never_claim_viewing(self):
        import shutil
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
            "TRANSLATION_FLOW_TOOL_ROOT": "", "TRANSLATION_FLOW_JASNA": "", "TRANSLATION_FLOW_ACCEPTANCE_REPORT": ""
        }):
            root = Path(raw)
            (root / "sample.mp4").write_bytes(b"path-only input; never decoded")
            tool_root = root / "tools"
            jasna = root / "custom-jasna/jasna.exe"
            for item in inspect_environment(mode="picture", tools=tool_root, jasna=jasna)["checks"]:
                if not item["present"]:
                    path = Path(item["path"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"non-executable path fixture")
            with self.isolated_queue(root) as (project, state, _, worker):
                shutil.copytree(frontend.ROOT / "src/jasna_picture", project / "src/jasna_picture", ignore=shutil.ignore_patterns("__pycache__"))
                self.run_ui(root, r"""
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    function script:Start-AuthorizedBatch {
        $script:CollectQueueRequests=$true
        try { Start-ConfirmedRun; $script:CapturedPicture=$script:CollectedQueueItems[0] }
        finally { $script:CollectQueueRequests=$false }
    }
    $BrowseVideoButton=New-Object Windows.Controls.Button
    $BrowseVideoButton.Add_Click({Set-SelectedFiles @('TESTROOT\sample.mp4')})
    (Get-Control 'PictureSampleButton').RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ((Get-SelectedTaskMode) -ne 'picture_only' -or $script:UiPage -ne 'setup') { throw 'sample entry did not open picture setup' }
    $OutputDirectoryBox.Text='TESTROOT\output'
    $ToolRootBox.Text='TESTROOT\tools'
    $JasnaPathBox.Text='TESTROOT\missing-jasna.exe'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1; return
}
if ($script:TestStep -eq 1) {
    if ($null -ne $script:AccessBridge -or -not $InputErrorText.Text) { return }
    if ($InputErrorText.Text -notmatch 'jasna' -or $InputErrorText.Text -match 'acceptance_report') { throw ('wrong first-use error: '+$InputErrorText.Text) }
    $JasnaPathBox.Text='TESTROOT\custom-jasna\jasna.exe'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=2; return
}
if ($null -ne $script:AccessBridge) { return }
if ($InputErrorText.Text) { throw $InputErrorText.Text }
if (-not (Get-Variable CapturedPicture -Scope Script -ErrorAction SilentlyContinue)) { return }
$item=$script:CapturedPicture
if ($item.jasna_path -ne 'TESTROOT\custom-jasna\jasna.exe' -or $item.tool_root -ne 'TESTROOT\tools' -or $item.python_path -ne $PythonPathBox.Text) { throw 'submitted runtime differs from readiness settings' }
$request=[IO.File]::ReadAllText($item.request_path) | ConvertFrom-Json
if ($request.gates.picture_and_mux_30m_human_accepted -ne $false -or $request.PSObject.Properties['provider']) { throw 'picture request invented consent or viewing acceptance' }
if ($script:QueueSnapshot.items.Count) { throw 'path-only fixture launched media' }
$script:TestResult=@{status='passed'}
""".replace("TESTROOT", str(root)), state=state, project=project, mock_readiness=False)

    def test_late_detail_poll_cannot_change_selected_stop_target(self):
        with tempfile.TemporaryDirectory() as raw:
            self.run_ui(Path(raw), r"""
function script:Start-QueueCall($Payload) { $script:CapturedAction=$Payload; return $true }
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$a=[pscustomobject]@{id='a';request_path='a/request.json';source='a.mp4';status='running';mode='picture_only';stage='picture_workflow';progress=20}
$b=[pscustomobject]@{id='b';request_path='b/request.json';source='b.mp4';status='stopped';mode='picture_only';stage='picture_workflow';progress=10}
$queue=[pscustomobject]@{project_root=$ProjectRoot;items=@($a,$b);paused=$false}
$QueueGrid.ItemsSource=@([pscustomobject]@{id='a';Item=$a},[pscustomobject]@{id='b';Item=$b})
$QueueGrid.SelectedIndex=1
$ViewQueueItemButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
Apply-QueueSnapshot ([pscustomobject]@{Queue=$queue;Watched=$a;Events=@()})
if ($script:CurrentQueueId -ne 'b') { throw 'old poll retargeted selection' }
# Select A's active card and stop it; the action must carry its explicit ID.
$card=(Get-Control 'ActiveTasksHost').Children[0]
$card.FindName('Buttons').Children[1].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($script:CapturedAction.action -ne 'stop' -or $script:CapturedAction.id -ne 'a') { throw 'stop targeted another task' }
$script:TestResult=@{status='passed'}
""")
