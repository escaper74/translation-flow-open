from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from translation_flow.queue_cli import rpc

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "Start-TranslationFlow.ps1"


@unittest.skipUnless(os.name == "nt", "WPF is Windows-only")
class FrontendPresentationTests(unittest.TestCase):
    def run_ui(self, directory, body, *, state=None, project=None, mock_readiness=True, mock_import=True, utf8_console=False, allow_queue_errors=False):
        source = UI.read_text(encoding="utf-8-sig").replace("$PSScriptRoot", "'" + str(UI.parent).replace("'", "''") + "'")
        source = source.replace("\nInitialize-QueueUi\n", "\nfunction Start-CodexStatusCheck { $script:CodexStatus='available' }\nInitialize-QueueUi\n")
        if mock_readiness:
            # The existing queue scenarios isolate scheduling from service calls.
            source = source.replace('[void]$Window.ShowDialog()', 'function Start-CheckedBatch { Start-AuthorizedBatch }\n[void]$Window.ShowDialog()')
        if mock_import:
            source = source.replace('[void]$Window.ShowDialog()', 'function Start-SubtitleImportCheck {}\n[void]$Window.ShowDialog()')
        injection = r'''
$Window.ShowInTaskbar=$false
$Window.ShowActivated=$false
$Window.WindowStartupLocation='Manual'
$Window.Left=-10000
$Window.Top=-10000
$script:TestResult=$null
$script:TestStep=0
$script:TestStarted=[DateTime]::UtcNow
$script:TestTimer=New-Object Windows.Threading.DispatcherTimer
$script:TestTimer.Interval=[TimeSpan]::FromMilliseconds(100)
$script:TestTimer.Add_Tick({
    try {
        $queueError=Get-Variable QueueLastError -ValueOnly -ErrorAction SilentlyContinue
        if ($queueError) { throw $queueError }
        if (([DateTime]::UtcNow-$script:TestStarted).TotalSeconds -gt 20) { throw ('UI timeout: '+$QueueStatusText.Text+' / '+$InputErrorText.Text+' / '+(Get-Variable QueueLastError -ValueOnly -ErrorAction SilentlyContinue)) }
        BODY
    } catch { $script:TestResult=@{status='failed';error=$_.Exception.Message;line=$_.InvocationInfo.ScriptLineNumber} }
    if ($null -ne $script:TestResult) { $script:TestTimer.Stop();$script:QueueSubmitting=$false;$script:IsRunning=$false;$Window.Close() }
})
$script:TestTimer.Start()
[void]$Window.ShowDialog()
$script:TestResult|ConvertTo-Json -Depth 5 -Compress
'''.replace("BODY", body)
        if allow_queue_errors:
            injection = injection.replace("if ($queueError) { throw $queueError }", "")
        source = source.replace("[void]$Window.ShowDialog()", injection)
        if utf8_console:
            source = source.replace('$ErrorActionPreference = "Stop"', '$ErrorActionPreference = "Stop"\n[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($true)')
        script = directory / f"ui-{time.time_ns()}.ps1"
        script.write_text(source, encoding="utf-8-sig")
        args = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File", str(script),
                "-ProjectRoot", str(project or directory), "-PythonPath", sys.executable]
        if state:
            args += ["-QueueStateDirectory", str(state)]
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        value = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(value["status"], "passed", value)
        return value

    @contextmanager
    def isolated_queue(self, root):
        state = root / "queue-state"
        project = root / "project"
        shutil.copytree(ROOT / "translation_flow", project / "translation_flow", ignore=shutil.ignore_patterns("__pycache__"))
        task_queue = project / "translation_flow/task_queue.py"
        task_queue.write_text(task_queue.read_text(encoding="utf-8").replace(
            "self.launcher = launcher or self._launch",
            "self.launcher = launcher or (lambda *args: (_ for _ in ()).throw(RuntimeError('offline fixture worker required')))"), encoding="utf-8")
        fixture = project / "tests/fixtures/frontend_queue_worker.py"
        fixture.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "tests/fixtures/frontend_queue_worker.py", fixture)
        workers = []

        def start():
            worker = subprocess.Popen([sys.executable, str(fixture), "serve", str(state)],
                cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            workers.append(worker)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    if rpc(state, {"action": "status"})["queue"]["worker_pid"] == worker.pid:
                        return worker
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(0.05)
            self.fail("isolated worker did not start")

        try:
            yield project, state, start, start()
        finally:
            try:
                rpc(state, {"action": "stop"})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if not any(row["status"] in {"starting", "running", "stopping"} for row in rpc(state, {"action": "status"})["queue"]["items"]):
                        rpc(state, {"action": "shutdown"})
                        break
                    time.sleep(0.05)
            finally:
                for worker in workers:
                    try:
                        worker.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        worker.terminate()  # Only this test's fixture worker.
                        worker.wait(timeout=5)

    def test_wpf_restart_start_batch_chains_success_failure_and_resume(self):
        from test_task_queue import make_request
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("previous", "next", "fail", "last"):
                (root / f"{name}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            with self.isolated_queue(root) as (project, state, start, worker):
                self.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\previous.srt')
    $OutputDirectoryBox.Text='TESTROOT'
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ([string]$ConsentOverlay.Visibility -ne 'Visible') { throw 'start confirmation missing' }
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1
    return
}
if ($script:QueueSnapshot.items.Count -ne 1 -or $script:QueueSnapshot.items[0].status -ne 'completed') { return }
$script:TestResult=@{status='passed'}
'''.replace("TESTROOT", str(root)), state=state, project=project, utf8_console=True)
                # Preserve one explicitly paused old task across a real worker restart.
                rpc(state, {"action": "pause"})
                held = make_request(root, "held")
                held_bytes = held.read_bytes()
                held_id = rpc(state, {"action": "enqueue", "authorized": True,
                                     "items": [{"request_path": str(held)}]})["queue"]["submission"]["added"][0]
                rpc(state, {"action": "shutdown"})
                worker.wait(timeout=8)
                start()
                self.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    if (-not $script:QueueSnapshot.paused -or $script:QueueSnapshot.items[0].status -ne 'completed') { throw 'restart precondition missing' }
    Set-SelectedFiles @('TESTROOT\next.srt','TESTROOT\fail.srt','TESTROOT\last.srt')
    $OutputDirectoryBox.Text='TESTROOT'
    # Ensure the click also covers mutation deferred behind an in-flight poll.
    [void](Start-QueueCall @{action='status'})
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ([string]$ConsentOverlay.Visibility -ne 'Visible') { throw 'batch confirmation missing' }
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($null -eq $script:PendingQueuePayload -or $script:PendingQueuePayload.action -ne 'enqueue') { throw 'test did not defer submission behind poll' }
    $script:TestStep=1
    return
}
$items=@($script:QueueSnapshot.items)
if ($items[1].status -ne 'paused') { throw 'new Start released an old paused task' }
if ($items.Count -ne 5 -or $items[4].status -ne 'completed') { return }
if (($items.status -join ',') -ne 'completed,paused,completed,failed,completed') { throw ('batch did not continue: '+($items.status -join ',')) }
if ($items[3].detail -ne 'offline fixture failure') { throw 'failed item lost cause' }
$script:TestResult=@{status='passed'}
'''.replace("TESTROOT", str(root)), state=state, project=project, utf8_console=True)
                snapshot = rpc(state, {"action": "status"})["queue"]
                self.assertFalse((held.parent / "events.jsonl").exists())
                for first, second in ((2, 3), (3, 4)):
                    self.assertLessEqual(snapshot["items"][first]["finished_at"], snapshot["items"][second]["started_at"])
                self.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    # Present the legacy worker's queued + global-pause shape through real cards.
    $legacy=$script:QueueSnapshot
    $held=@($legacy.items | Where-Object {$_.id -eq 'HELDID'})[0]
    $legacy.paused=$true;$held.status='queued'
    Apply-QueueSnapshot ([pscustomobject]@{Queue=$legacy;Watched=$held;Events=@()})
    $card=@($Window.FindName('RecoverTasksHost').Children | Where-Object {$_.FindName('Title').Text -eq 'held.ja.srt'})[0]
    if ($card.FindName('Status').Text -notmatch '已暂停') { throw 'paused task falsely says waiting' }
    if ($Window.FindName('WaitingTasksHost').Children.Count) { throw 'globally paused row is in waiting section' }
    $card.FindName('Buttons').Children[0].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($script:PendingQueueResumeId -ne 'HELDID' -or [string]$ConsentOverlay.Visibility -ne 'Visible') { throw 'paused card cannot resume original request' }
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $script:TestStep=1
    return
}
$held=@($script:QueueSnapshot.items | Where-Object {$_.id -eq 'HELDID'})[0]
if ($held.status -ne 'completed') { return }
if ($script:QueueSnapshot.items[3].status -ne 'failed') { throw 'resuming selected task retried another failure' }
$script:TestResult=@{status='passed'}
'''.replace("HELDID", held_id), state=state, project=project, utf8_console=True)
                self.assertEqual(held_bytes, held.read_bytes())

    def test_wpf_queue_rejection_displays_cause_and_releases_start_button(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            with self.isolated_queue(root) as (project, state, _, worker):
                self.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\source.srt')
    $OutputDirectoryBox.Text='TESTROOT'
    [void](Start-QueueCall @{action='status'})
    $StartButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    $ConfirmConsentButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($null -eq $script:PendingQueuePayload) { throw 'no deferred payload for fixture fault' }
    # Explicit fault injection before the real bridge/RPC, never a production file.
    $script:PendingQueuePayload.items[0].request_path='TESTROOT\missing-request.json'
    $script:TestStep=1
    return
}
if (-not (Get-Variable QueueLastError -ValueOnly -ErrorAction SilentlyContinue)) { return }
if ($InputErrorText.Text -notmatch 'missing-request.json' -or $InputErrorText.Text -notmatch '后台未确认开始') { throw ('queue error cause hidden: '+$InputErrorText.Text) }
if ($script:QueueSubmitting -or -not $StartButton.IsEnabled -or $script:UiPage -ne 'setup') { throw 'failed submission leaves Start blocked' }
if ($script:QueueSnapshot.items.Count) { throw 'rejected request was launched' }
$script:TestResult=@{status='passed'}
'''.replace("TESTROOT", str(root)), state=state, project=project, allow_queue_errors=True)

    def test_feature_home_auto_routes_inputs_and_keeps_remux_separate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'sample.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n', encoding='utf-8')
            (root / 'sample.mp4').write_bytes(b'offline path fixture, not media')
            body = r'''
if ([string]$TaskModeBox.Visibility -ne 'Collapsed') { throw 'internal modes still exposed' }
if (-not (Get-Control 'PictureFeatureButton').IsEnabled) { throw 'picture is unavailable' }
$script:BeautyOpened=$false
function Show-BeautyPlayer { $script:BeautyOpened=$true }
if (-not (Get-Control 'BeautyPlayerButton').IsVisible) { throw 'beauty launcher is hidden behind tools' }
$ancestor=(Get-Control 'BeautyPlayerButton').Parent
while ($null -ne $ancestor) {
    if ($ancestor -is [Windows.Controls.Expander]) { throw 'beauty launcher is inside an expander' }
    $ancestor=[Windows.LogicalTreeHelper]::GetParent($ancestor)
}
(Get-Control 'BeautyPlayerButton').RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
if (-not $script:BeautyOpened) { throw 'beauty launcher button is not wired' }
Set-SelectedFiles @('TESTROOT\sample.srt')
if ((Get-SelectedTaskMode) -ne 'srt_translation' -or $SelectedFunctionText.Text -notmatch '字幕文件') { throw 'SRT was not auto-routed' }
if ($StartButton.Content -ne '开始处理') { throw 'start action exposes internal routing' }
Set-SelectedFiles @('TESTROOT\sample.mp4')
if ((Get-SelectedTaskMode) -ne 'translation' -or $SelectedFunctionText.Text -notmatch '视频文件') { throw 'video was not auto-routed after SRT' }
$RepackageToolButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
if ((Get-SelectedTaskMode) -ne 'media_repackage' -or [string]$ProviderPanel.Visibility -ne 'Collapsed') { throw 'remux tool did not switch or still asks for provider' }
if ($StartButton.Content -ne '开始换格式' -or -not $MoreSettings.IsExpanded) { throw 'remux settings are hidden' }
Set-SelectedFiles @('TESTROOT\sample.srt')
if ((Get-SelectedTaskMode) -ne 'media_repackage' -or $InputVideoBox.Text -notmatch 'sample.mp4$' -or $InputErrorText.Text -notmatch '精修中文字幕') { throw 'remux SRT drop silently changed purpose' }
$SubtitleFeatureButton.IsChecked=$true
$SubtitleFeatureButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
Set-SelectedFiles @('TESTROOT\sample.srt')
if ((Get-SelectedTaskMode) -ne 'srt_translation' -or [string]$ProviderPanel.Visibility -ne 'Visible') { throw 'subtitle feature did not restore routing' }
Set-InputEnabled $false
if ($SubtitleFeatureButton.IsEnabled -or $RepackageToolButton.IsEnabled) { throw 'feature can change during processing' }
Set-InputEnabled $true
if (-not (Get-Control 'PictureFeatureButton').IsEnabled) { throw 'picture did not re-enable' }
$NewTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
if ((Get-SelectedTaskMode) -ne 'translation' -or $InputVideoBox.Text -or $StartButton.IsEnabled) { throw 'new task retained old input route' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root).replace("'", "''"))
            self.run_ui(root, body)

    def test_mixed_subtitle_inputs_create_correct_formal_requests(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'sample.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n', encoding='utf-8')
            (root / 'sample.mp4').write_bytes(b'offline path fixture, not media')
            body = r'''
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
function Start-QueueCall($Payload) { $script:TestPayload=$Payload; return $true }
function Start-TranslationFlowBackend { throw 'must not start a backend in input-routing test' }
Set-SelectedFiles @('TESTROOT\sample.srt','TESTROOT\sample.mp4')
if ($SelectedFunctionText.Text -notmatch '多个文件') { throw 'mixed input summary missing' }
$OutputDirectoryBox.Text='TESTROOT\results'
$script:CodexStatus='available'
Start-AuthorizedBatch
if ($script:TestPayload.action -ne 'enqueue' -or $script:TestPayload.items.Count -ne 2) { throw 'mixed batch lost requests' }
$first=Get-Content -LiteralPath $script:TestPayload.items[0].request_path -Raw -Encoding UTF8 | ConvertFrom-Json
$second=Get-Content -LiteralPath $script:TestPayload.items[1].request_path -Raw -Encoding UTF8 | ConvertFrom-Json
if ($first.workflow_mode -ne 'srt_translation' -or $first.input_srt -notmatch 'sample.srt$') { throw 'SRT request mismatch' }
if ($second.input_video -notmatch 'sample.mp4$' -or $second.PSObject.Properties.Name -contains 'workflow_mode') { throw 'video request routed into another workflow' }
if ($first.provider.id -ne 'codex' -or $second.provider.id -ne 'codex') { throw 'auto routing changed provider' }
if ($null -ne $script:Bridge) { throw 'routing started a backend' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root).replace("'", "''"))
            self.run_ui(root, body)

    def test_main_theme_survives_feature_switches_and_progress_reset(self):
        with tempfile.TemporaryDirectory() as raw:
            self.run_ui(Path(raw), r'''
function Assert-ThemeColor($Brush, [string]$Color, [string]$Name) {
    if ($Brush.Color.ToString() -ne ('#FF'+$Color.TrimStart('#'))) { throw ('wrong theme: '+$Name+' '+$Brush) }
}
function Save-ThemeSnapshot([Windows.Media.Visual]$Visual, [string]$Name) {
    if (-not $env:TRANSLATION_FLOW_UI_EVIDENCE) { return }
    $Window.UpdateLayout()
    $size=$Visual.RenderSize
    $bitmap=New-Object Windows.Media.Imaging.RenderTargetBitmap ([int][Math]::Ceiling($size.Width)),([int][Math]::Ceiling($size.Height)),96,96,([Windows.Media.PixelFormats]::Pbgra32)
    $bitmap.Render($Visual)
    $encoder=New-Object Windows.Media.Imaging.PngBitmapEncoder
    $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
    $file=[IO.File]::Open((Join-Path $env:TRANSLATION_FLOW_UI_EVIDENCE ($Name+'.png')),[IO.FileMode]::CreateNew)
    try { $encoder.Save($file) } finally { $file.Dispose() }
}
Assert-ThemeColor $Window.Background '#FCFDFE' 'main background'
Assert-ThemeColor $StartButton.Background '#00A5A2' 'start button'
Assert-ThemeColor $SubtitleFeatureButton.BorderBrush '#00A5A2' 'selected feature'
Assert-ThemeColor $SubtitleFeatureButton.Background '#ECFAF9' 'selected feature fill'
Assert-ThemeColor $DropZoneInner.Background '#F2FAFA' 'file drop surface'
foreach($name in @('HomePictureButton','HomeTranslationButton','BeautyPlayerButton')) {
    if (-not (Get-Control $name).IsVisible) { throw ('home action hidden: '+$name) }
}
Save-ThemeSnapshot $Window.Content 'home'
Set-UiPage 'setup'
$Window.UpdateLayout()
$startOrigin=$StartButton.TransformToAncestor($MainScroll).Transform((New-Object Windows.Point 0,0))
if ($startOrigin.Y+$StartButton.ActualHeight -gt $MainScroll.ActualHeight) { throw 'start button clipped at default window size' }
$InputErrorText.Text='示例错误'
if ([string]$InputErrorText.Visibility -ne 'Visible') { throw 'validation error is hidden' }
$InputErrorText.Text=''
if ($ProviderBox -isnot [Windows.Controls.ListBox] -or $ProfileBox -isnot [Windows.Controls.ListBox] -or $OutputFormatBox -isnot [Windows.Controls.ListBox]) { throw 'common choices regressed to dropdowns' }
(Get-Control 'SettingsButton').RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
$ProviderBox.UpdateLayout()
$selected=$ProviderBox.ItemContainerGenerator.ContainerFromIndex($ProviderBox.SelectedIndex)
[void]$selected.ApplyTemplate()
$surface=$selected.Template.FindName('ItemSurface',$selected)
Assert-ThemeColor $surface.Background '#00A5A2' 'selected service'
Assert-ThemeColor $selected.Foreground '#FFFFFF' 'selected service text'
Save-ThemeSnapshot $Window.Content 'settings'
(Get-Control 'CloseSettingsButton').RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
$PictureFeatureButton.IsChecked=$true
$PictureFeatureButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
Assert-ThemeColor $PictureFeatureButton.BorderBrush '#00A5A2' 'dynamic picture selection'
Assert-ThemeColor $PictureSelectedText.Foreground '#00A5A2' 'dynamic selection label'
$SubtitleFeatureButton.IsChecked=$false
$SubtitleFeatureButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
Assert-ThemeColor $SubtitleFeatureButton.BorderBrush '#E3E9EF' 'unselected feature'
Assert-ThemeColor $SubtitleSelectedText.Foreground '#778599' 'unselected label'
$script:CurrentWorkflowMode='translation'
Reset-RunPresentation
Set-StageIndicators 'translate_initial'
Set-ProgressValue 42
Assert-ThemeColor $ProgressBar.Foreground '#00A5A2' 'reset progress'
Assert-ThemeColor $TranslateDot.Background '#00A5A2' 'active stage'
Assert-ThemeColor $ExportDot.Background '#DDF2F1' 'pending stage'
$RunStateText.Text='正在精修中文字幕'
$RunSourceText.Text='示例影片.mp4'
$StageText.Text='正在翻译字幕'
# Settle the synthetic progress target for the still image, without waiting for animation.
$ProgressBar.BeginAnimation([Windows.Controls.Primitives.RangeBase]::ValueProperty,$null)
$ProgressBar.Value=$script:CurrentProgress
Save-ThemeSnapshot $Window.Content 'running'
Set-UiPage 'tasks'
Assert-ThemeColor ($Window.FindResource([Windows.SystemColors]::HighlightBrushKey)) '#00A5A2' 'task selection'
Save-ThemeSnapshot $Window.Content 'tasks'
if ($null -ne $script:Bridge) { throw 'theme test started a backend' }
$script:TestResult=@{status='passed'}
''')

    def test_task_cards_route_actions_by_id_and_asr_has_no_fake_percent(self):
        with tempfile.TemporaryDirectory() as raw:
            self.run_ui(Path(raw), r'''
$active=[pscustomobject]@{id='active-id';request_path='active/request.json';source='active.mp4';mode='translation';status='running';stage='asr_a';progress=3}
$stopped=[pscustomobject]@{id='stopped-id';request_path='stopped/request.json';source='stopped.mp4';mode='picture_only';status='stopped'}
$script:QueueSnapshot=[pscustomobject]@{items=@($active,$stopped)}
$QueueGrid.ItemsSource=@([pscustomobject]@{id=$active.id;Item=$active},[pscustomobject]@{id=$stopped.id;Item=$stopped})
Update-DesignTaskCards
Set-UiPage 'tasks'
$card=(Get-Control 'ActiveTasksHost').Children[0]
if (-not $card.FindName('Progress').IsIndeterminate -or $card.FindName('Percent').Text -ne '识别中') { throw 'ASR card fabricated audio progress' }
$card.FindName('Buttons').Children[0].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($script:CurrentQueueId -ne 'active-id' -or $script:UiPage -ne 'run') { throw 'details opened wrong task' }
function script:Start-QueueCall($Payload) { $script:CapturedQueueAction=$Payload;return $true }
$card.FindName('Buttons').Children[1].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($script:CapturedQueueAction.action -ne 'stop' -or $script:CapturedQueueAction.id -ne 'active-id') { throw 'stop escaped selected task' }
if ((Get-Control 'RecoverTasksHost').Children.Count -ne 1) { throw 'stopped task missing' }
$script:TestResult=@{status='passed'}
''')

    def test_products_embed_and_keep_selected_media_and_versions(self):
        from tests.test_delivery_library import fixture
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            output=root/'results'; output.mkdir()
            first=fixture(output,job='first')
            fixture(output,job='second')
            result_path=first.parent/'workflow.result.json'
            result=json.loads(result_path.read_text())
            alternate=output/'first.final.mp4'; alternate.write_bytes(b'isolated UI fixture')
            result['outputs']['final_mp4']=str(alternate)
            result_path.write_text(json.dumps(result))
            self.run_ui(root, r'''
if ($script:TestStep -eq 0) {
    $ProjectRoot='REPO'
    $OutputDirectoryBox.Text='OUTPUT'
    Show-DeliveryLibrary
    $script:TestStep=1
    return
}
$state=$script:EmbeddedLibraryState
if ($null -ne $state.Call) { return }
if ($state.Cards.Count -ne 2) { throw ('missing product cards: '+$state.Status.Text+' '+$state.Status.ToolTip) }
if ($script:UiPage -ne 'library' -or (Get-Control 'LibraryPageHost').Content -eq $null) { throw 'library did not embed' }
$state.Grid.SelectedItem=@($state.Grid.ItemsSource|Where-Object {$_.job_id -eq 'first'})[0]
if ([string]$state.Variants.SelectedItem -notmatch 'first.final.mp4$') { throw 'preferred MP4 lost' }
$state.Controls.LibraryVersionsButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
$state.Controls.LibraryVersionChoices.Children[1].RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ([string]$state.Variants.SelectedItem -notmatch 'first.final.mkv$') { throw 'alternate version did not select MKV' }
function script:Show-BeautyPlayer([string]$Source) { $script:SelectedBeautyPath=$Source }
$state.Controls.LibraryBeauty.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($script:SelectedBeautyPath -ne [string]$state.Variants.SelectedItem) { throw 'beauty opened wrong version' }
if ($null -ne $script:Bridge) { throw 'catalog opened a production backend' }
$script:TestResult=@{status='passed'}
'''.replace('REPO',str(ROOT)).replace('OUTPUT',str(output)))

    def test_eta_uses_current_phase_work_and_resets_on_phase_change(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            value = self.run_ui(root, r'''
$script:CurrentWorkflowMode='srt_translation'
Reset-RunPresentation
$start=[DateTimeOffset]::UtcNow.AddSeconds(-60)
Update-EtaObservation ([pscustomobject]@{stage='translate_initial';detail='2/10';timestamp_utc=$start.ToString('o')})
if ($script:EtaRange) { throw 'ETA appeared before measurable work' }
Update-EtaObservation ([pscustomobject]@{stage='translate_initial';detail='5/10';timestamp_utc=$start.AddSeconds(60).ToString('o')})
if ($script:EtaRange -notmatch '分钟') { throw 'ETA missing after measurable progress' }
$range=$script:EtaRange
Update-EtaObservation ([pscustomobject]@{stage='polish';detail='1/10';timestamp_utc=$start.AddSeconds(61).ToString('o')})
if ($script:EtaRange) { throw 'ETA leaked across phases' }
Update-EtaObservation ([pscustomobject]@{stage='polish';overall_percent=95;timestamp_utc=$start.AddSeconds(62).ToString('o')})
if ($script:EtaRange) { throw 'Weighted overall percent was treated as elapsed time' }
Set-ProgressValue 40
Set-ProgressValue 10
if ($script:CurrentProgress -ne 40) { throw 'progress jumped backwards within a run' }
Reset-RunPresentation
if ($script:CurrentProgress -ne 0) { throw 'new run reused old percent' }
Set-FailureSummary 'connection timeout'
if ($RunStateText.Text -ne '暂时连不上翻译服务' -or $StageText.Text -match 'timeout') { throw 'raw error exposed in main heading' }
if ([string]$RecognizeStep.Visibility -ne 'Collapsed') { throw 'SRT claims ASR stage' }
Show-QueueSubmission ([pscustomobject]@{submission=[pscustomobject]@{skipped=@([pscustomobject]@{request_path='missing/request.json';message='fixture missing'})}})
if ([string]$QueueSubmissionText.Visibility -ne 'Visible' -or $QueueSubmissionText.Text -notmatch '1 个文件未加入') { throw 'partial submission was silent' }
$script:TestResult=@{status='passed';range=$range}
''')
            self.assertTrue(value["range"])

    def test_asr_stage_and_elapsed_stay_visible_without_progress_events(self):
        with tempfile.TemporaryDirectory() as raw:
            self.run_ui(Path(raw), r'''
$script:CurrentWorkflowMode='translation_picture_final'
$script:CurrentJobId='presentation-fixture'
$script:LastSeq=0
$script:IsRunning=$true
Reset-RunSummary
Reset-RunPresentation
$start=[DateTimeOffset]::UtcNow.AddSeconds(-392)
function Send-TestEvent($Name, $Stage, $Seconds, $Percent) {
    Handle-BackendEvent ([pscustomobject]@{schema_version=1;job_id=$script:CurrentJobId;seq=($script:LastSeq+1);event=$Name;stage=$Stage;timestamp_utc=$start.AddSeconds($Seconds).ToString('o');overall_percent=$Percent})
}
Send-TestEvent 'job_started' '' 0 0
Send-TestEvent 'stage_started' 'preflight' 0 0
Send-TestEvent 'stage_completed' 'preflight' 0.2 0.6
Send-TestEvent 'stage_started' 'media_input' 0.2 0.6
Send-TestEvent 'stage_completed' 'media_input' 0.6 1.2
Send-TestEvent 'stage_started' 'asr_a' 0.6 1.8
if ($RunStateText.Text -notmatch 'Whisper A' -or $StageText.Text -notmatch 'Whisper A') { throw 'actual ASR stage hidden' }
if ($ProgressText.Text -ne '识别中' -or [string]$ProgressBar.Visibility -ne 'Hidden') { throw 'stage weight presented as live recognition percent' }
if ($EtaText.Text -ne '暂无可靠估算') { throw 'ASR claims an estimate is being calculated' }
if ($ElapsedText.Text -notmatch '^06:3' -or $DurationSummaryText.Text -notmatch '^总耗时：6 分' -or $DurationSummaryText.Text -notmatch '当前：.*Whisper A') { throw ('elapsed mismatch: '+$DurationSummaryText.Text) }
# No new event arrives; the clock alone must refresh the details.
$script:JobStartedAt=$script:JobStartedAt.AddSeconds(-60)
$script:StageStartedAt['asr_a']=$script:StageStartedAt['asr_a'].AddSeconds(-60)
Update-ClockPresentation
if ($ElapsedText.Text -notmatch '^07:3' -or $DurationSummaryText.Text -notmatch '^总耗时：7 分' -or $DurationSummaryText.Text -notmatch '已用 7 分') { throw 'details froze at last event' }
Send-TestEvent 'stage_completed' 'asr_a' 390 26
Send-TestEvent 'stage_started' 'asr_c' 390 27
if ($StageText.Text -notmatch 'Whisper C' -or $ProgressText.Text -ne '识别中') { throw 'C stage kept A presentation' }
Send-TestEvent 'stage_completed' 'asr_c' 391 45
Send-TestEvent 'stage_started' 'translate_initial' 391 46
if ($ProgressText.Text -ne '46%' -or [string]$ProgressBar.Visibility -ne 'Visible' -or $StageText.Text -match 'Whisper') { throw 'next stage retained ASR presentation' }
Send-TestEvent 'job_failed' 'translate_initial' 392 46
Complete-Run $false 'fixture failure'
$frozen=$DurationSummaryText.Text
Update-ClockPresentation
if ($DurationSummaryText.Text -ne $frozen -or $DurationSummaryText.Text -match '当前：') { throw 'terminal duration kept running' }
$script:TestResult=@{status='passed'}
''')

    def test_wpf_batch_stop_close_reconnect_and_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "queue-state"
            # A crashed fixture must never fall back to a live provider or media job.
            project = root / "project"
            shutil.copytree(ROOT / "translation_flow", project / "translation_flow", ignore=shutil.ignore_patterns("__pycache__"))
            task_queue = project / "translation_flow/task_queue.py"
            queue_source = task_queue.read_text(encoding="utf-8")
            queue_source = queue_source.replace("self.launcher = launcher or self._launch", "self.launcher = launcher or (lambda *args: (_ for _ in ()).throw(RuntimeError('offline fixture worker required')))")
            task_queue.write_text(queue_source, encoding="utf-8")
            fixture = project / "tests/fixtures/frontend_queue_worker.py"
            fixture.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "tests/fixtures/frontend_queue_worker.py", fixture)
            for name in ("first", "second"):
                (root / f"{name}.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            worker = subprocess.Popen([sys.executable, str(fixture), "serve", str(state)],
                                      cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = time.monotonic()+8
                while True:
                    try:
                        rpc(state, {"action": "status"})
                        break
                    except Exception:
                        if time.monotonic()>deadline:
                            raise
                        time.sleep(0.1)
                body = r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    Set-SelectedFiles @('TESTROOT\first.srt','TESTROOT\second.srt')
    $OutputDirectoryBox.Text='TESTROOT'
    Select-ComboBoxItemByTag $ProviderBox 'deepseek'
    $ApiKeyBox.Password='offline-fixture-only'
    Begin-ConfiguredRun
    if ([string]$ConsentOverlay.Visibility -ne 'Visible' -or $ConsentDetailText.Text -notmatch '2 个文件') { throw 'batch consent missing' }
    $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    if ($ApiKeyBox.Password) { throw 'Key stayed in UI after submission' }
    $script:TestStep=1
    return
}
if ($script:TestStep -eq 1) {
    $items=@($script:QueueSnapshot.items)
    if ($items.Count -ne 2) { return }
    if ($items[0].status -eq 'failed') { throw $items[0].message }
    if ($items[0].status -ne 'running' -or $items[0].progress -le 0) { return }
    $StopRunButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    $script:TestStep=2
    return
}
$items=@($script:QueueSnapshot.items)
if ($items[0].status -ne 'stopped') { return }
$script:TestResult=@{status='passed';first=$items[0].id;second=$items[1].id;request=$items[0].request_path}
'''.replace("TESTROOT", str(root).replace("'", "''"))
                value = self.run_ui(root, body, state=state, project=project, utf8_console=True)
                deadline = time.monotonic()+12
                while True:
                    snapshot = rpc(state, {"action": "status"})["queue"]
                    if snapshot["items"][1]["status"] == "completed":
                        break
                    self.assertLess(time.monotonic(), deadline, snapshot)
                    time.sleep(0.1)
                self.assertEqual(snapshot["items"][0]["status"], "stopped")
                self.assertNotIn("offline-fixture-only", (state / "queue.json").read_text(encoding="utf-8"))
                request_before = Path(value["request"]).read_bytes()
                body = r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($script:TestStep -eq 0) {
    $QueueGrid.SelectedItem=@($QueueGrid.ItemsSource | Where-Object {$_.id -eq 'FIRSTID'})[0]
    $ResumeQueueItemButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    $CancelConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    $QueueGrid.SelectedItem=@($QueueGrid.ItemsSource | Where-Object {$_.id -eq 'FIRSTID'})[0]
    $ResumeQueueItemButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    if ((Get-SelectedProviderSettings).Id -ne 'deepseek') { throw 'restored provider changed' }
    if ($ApiKeyBox.Password -or -not (Test-QueueSessionKeyAvailable)) { throw 'session key was not reused after reopening' }
    if ([string]$ConsentOverlay.Visibility -ne 'Visible') { throw ('resume consent missing: '+$InputErrorText.Text) }
    Select-ComboBoxItemByTag $ProfileBox 'adult_direct'
    if ((Test-QueueResumeSettings) -notmatch '原设置') { throw 'resume accepted changed settings' }
    Select-ComboBoxItemByTag $ProfileBox 'normal'
    $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    $script:TestStep=1
    return
}
$first=@($script:QueueSnapshot.items | Where-Object {$_.id -eq 'FIRSTID'})[0]
if ($first.status -ne 'completed') { return }
if ($RunStateText.Text -ne '翻译好了') { throw ('completed UI mismatch: '+$RunStateText.Text+' / '+$StageText.Text) }
if ([string]$OpenResultButton.Visibility -ne 'Visible') { throw 'result action missing' }
$script:TestResult=@{status='passed'}
'''.replace("FIRSTID", value["first"])
                self.run_ui(root, body, state=state, project=project, utf8_console=True)
                self.assertEqual(request_before, Path(value["request"]).read_bytes())
            finally:
                try:
                    rpc(state, {"action": "stop"})
                    deadline=time.monotonic()+8
                    while time.monotonic()<deadline:
                        q=rpc(state, {"action": "status"})["queue"]
                        if not any(i["status"] in {"starting","running","stopping"} for i in q["items"]):
                            break
                        time.sleep(0.1)
                    rpc(state, {"action": "shutdown"})
                    worker.wait(timeout=8)
                finally:
                    if worker.poll() is None:
                        worker.terminate()
                        worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
