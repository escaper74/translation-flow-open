"""Product routing, no-service picture jobs, and safe picture checkpoints."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from full_workflow.contracts import FullWorkflowRequest
from full_workflow.picture import PictureRequest, PictureRunner
from translation_flow.desktop_access import check_readiness
from translation_flow.task_queue import request_info, TaskQueue
from translation_flow.cancellation import TranslationCancelled
import test_frontend_presentation as frontend


class ProductDeliveryTests(unittest.TestCase):
    def test_picture_queue_has_no_translation_identity_or_credential_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mkv'
            source.write_bytes(b'path-only fixture')
            for mode, directory in [('picture_only', '.picture-workflow'), ('translation_picture_final', '.full-workflow')]:
                raw = dict(schema_version=1, workflow_mode=mode, job_id='product-test', input_video=str(source),
                           output_dir=str(root), gates=dict(picture_and_mux_30m_human_accepted=True))
                if mode == 'picture_only':
                    raw['output_target'] = 'mp4'
                    PictureRequest.from_mapping(raw)
                else:
                    raw.update(profile_id='normal', provider=dict(id='codex', host='chatgpt.com', model='cli-default'),
                               consent=dict(current_run=True, subtitle_text_only=True, audio_video_local=True))
                    FullWorkflowRequest.from_mapping(raw)
                path = root / directory / 'product-test/request.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(raw), encoding='utf-8')
                info = request_info(path)
                self.assertEqual('' if mode == 'picture_only' else 'codex', info['provider'])
                self.assertEqual('full_workflow.picture_cli' if mode == 'picture_only' else 'full_workflow.cli', info['module'])
                if mode == 'picture_only':
                    queue = TaskQueue(root / 'queue')
                    reply = queue.command({'action': 'enqueue', 'items': [{'request_path': str(path)}]})
                    self.assertEqual('queued', reply['items'][0]['status'])

    def test_picture_readiness_never_checks_login_or_translation(self):
        with patch('translation_flow.desktop_access.inspect_environment', return_value={'checks': []}), \
             patch('translation_flow.desktop_access.codex_status', side_effect=AssertionError('no login')), \
             patch('translation_flow.desktop_access.create_api_provider', side_effect=AssertionError('no API')):
            self.assertEqual('ready', check_readiness(mode='picture', provider='codex', probe=True)['status'])

    def test_picture_safe_stop_completes_one_segment_then_resumes(self):
        from jasna_picture import workflow
        config = Mock(scope='full', source=Path('source'), gate_report=Path('gate'), manifest_path=Path('manifest'))
        stop = [False]
        processed = []
        def process(_config, path, **_):
            processed.append(path.name)
            stop[0] = True
            return {'completed': path.name}
        with patch.object(workflow, '_require_gate', return_value={}), \
             patch.object(workflow, 'preflight_workflow', return_value={'source_duration_seconds': 4}), \
             patch.object(workflow, '_gpu_utilization_percent', return_value=None), \
             patch.object(workflow, '_load_or_create_manifest', return_value={}), \
             patch.object(workflow, '_prepare_segments', return_value=([{'path':'one'},{'path':'two'}], {})), \
             patch.object(workflow, '_process_segment', side_effect=process), \
             patch.object(workflow, '_concat_and_verify') as concat:
            with self.assertRaises(TranslationCancelled):
                workflow.run_picture_workflow(config, cancelled=lambda: stop[0])
            self.assertEqual(['one'], processed)
            concat.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'WPF is Windows-only')
class ProductUiTests(unittest.TestCase):
    def test_history_resume_button_submits_request_only_video_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sample.mp4').write_bytes(b'path fixture only')
            body = r'''
function Start-QueueCall($Payload) { $script:TestPayload=$Payload; return $true }
function Start-DesktopAccess { throw 'history resume must use existing backend checks' }
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$script:CodexStatus='available'
$OutputDirectoryBox.Text='TESTROOT\out'
Set-SelectedFiles @('TESTROOT\sample.mp4')
foreach ($mode in @('translation','translation_picture_final')) {
    Select-ComboBoxItemByTag $TaskModeBox $mode
    $script:CollectQueueRequests=$true
    Start-ConfirmedRun
    $path=$script:CollectedQueueItems[$script:CollectedQueueItems.Count-1].request_path
    $original=[IO.File]::ReadAllText($path)
    $script:CollectQueueRequests=$false
    foreach ($snapshot in @($null, [pscustomobject]@{items=@()})) {
        $script:QueueSnapshot=$snapshot
        $script:TestPayload=$null
        $script:QueueSubmitting=$false
        $ShowTasksButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        $TaskHistoryGrid.SelectedItem=@($TaskHistoryGrid.ItemsSource | Where-Object RequestPath -eq $path)[0]
        if (-not $ResumeTaskButton.IsEnabled) { throw 'request-only history resume is disabled' }
        $ResumeTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        if ($InputErrorText.Text -or [string]$ConsentOverlay.Visibility -ne 'Visible') { throw ('resume click did not reach consent: '+$InputErrorText.Text) }
        if ((Get-SelectedTaskMode) -ne $mode) { throw 'resume changed task mode' }
        $ConfirmConsentButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
        if ($InputErrorText.Text) { throw $InputErrorText.Text }
        if ($null -eq $script:TestPayload -or $script:TestPayload.action -ne 'enqueue' -or $script:TestPayload.items.Count -ne 1 -or $script:TestPayload.items[0].request_path -ne $path) { throw 'confirmed history resume did not submit exactly the original task' }
        if ([IO.File]::ReadAllText($path) -ne $original) { throw 'history resume changed the original request' }
    }
}
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root))
            frontend.FrontendPresentationTests().run_ui(root, body, mock_readiness=False, utf8_console=True)

    def test_request_only_retry_with_missing_empty_and_single_queue_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sample.mp4').write_bytes(b'path fixture only')
            body = r'''
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$script:CollectQueueRequests=$true
$script:CodexStatus='available'
$OutputDirectoryBox.Text='TESTROOT\out'
Set-SelectedFiles @('TESTROOT\sample.mp4')
foreach ($mode in @('translation','translation_picture_final')) {
    $SubtitleFeatureButton.IsChecked=$true
    $PictureFeatureButton.IsChecked=$mode -eq 'translation_picture_final'
    Sync-FeatureMode
    $script:QueueSnapshot=$null
    Start-ConfirmedRun
    $path=$script:CollectedQueueItems[$script:CollectedQueueItems.Count-1].request_path
    $original=[IO.File]::ReadAllText($path)
    foreach ($snapshot in @($null, [pscustomobject]@{items=@()}, [pscustomobject]@{items=@([pscustomobject]@{request_path=$path})})) {
        $script:QueueSnapshot=$snapshot
        Start-ConfirmedRun
        if ([IO.File]::ReadAllText($path) -ne $original) { throw 'retry changed original request' }
    }
    $entry=@(Get-TaskCenterEntries $OutputDirectoryBox.Text | Where-Object RequestPath -eq $path)[0]
    if ($entry.StatusLabel -ne '请求已保存 / 无运行记录' -or $entry.FailureText -notmatch '后台') { throw 'request-only task hides missing execution evidence' }
    $script:QueueSnapshot=[pscustomobject]@{items=@([pscustomobject]@{request_path=$path})}
    $changed=$original.Replace('"additional_mp4":  false','"additional_mp4":  true')
    if ($changed -eq $original) { throw 'fixture failed to change delivery' }
    [IO.File]::WriteAllText($path,$changed)
    $blocked=$false
    try { Start-ConfirmedRun } catch { $blocked=$_.Exception.Message -match '不能改写' }
    if (-not $blocked -or [IO.File]::ReadAllText($path) -ne $changed) { throw 'queued request protection failed' }
}
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root))
            frontend.FrontendPresentationTests().run_ui(root, body)

    def test_three_combinations_emit_formal_requests_and_keep_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sample.mp4').write_bytes(b'path fixture only')
            (root / 'sample.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
            body = r'''
function Start-CodexStatusCheck { $script:CodexStatus='available' }
$script:CodexStatus='available'
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$script:CollectQueueRequests=$true
$OutputDirectoryBox.Text='TESTROOT\out'
Set-SelectedFiles @('TESTROOT\sample.mp4')
foreach ($mode in @('translation','picture_only','translation_picture_final')) {
    $SubtitleFeatureButton.IsChecked=$mode -ne 'picture_only'
    $PictureFeatureButton.IsChecked=$mode -ne 'translation'
    Sync-FeatureMode
    if ((Get-SelectedTaskMode) -ne $mode) { throw "wrong route $mode" }
    if ([string]$ProviderBox.SelectedItem.Tag -ne 'codex') { throw 'provider changed during feature selection' }
    if ($mode -eq 'picture_only') {
        $script:CodexStatus='login_required'
        if ([string]$ProviderPanel.Visibility -ne 'Collapsed' -or [string]$CodexStatusPanel.Visibility -ne 'Collapsed') { throw 'picture asks for service' }
    }
    if (Test-RunInputs) { throw (Test-RunInputs) }
    Start-ConfirmedRun
    $request=[IO.File]::ReadAllText($script:CollectedQueueItems[$script:CollectedQueueItems.Count-1].request_path) | ConvertFrom-Json
    if ($mode -eq 'picture_only' -and $request.PSObject.Properties['provider']) { throw 'picture has provider identity' }
    $script:CodexStatus='available'
}
if ($script:CollectedQueueItems.Count -ne 3) { throw 'missing formal requests' }
$SubtitleFeatureButton.IsChecked=$false; $PictureFeatureButton.IsChecked=$false; Sync-FeatureMode
if ((Test-RunInputs) -notmatch '至少') { throw 'empty feature selection accepted' }
$SubtitleFeatureButton.IsChecked=$true; $PictureFeatureButton.IsChecked=$true; Sync-FeatureMode
Set-SelectedFiles @('TESTROOT\sample.srt')
if ((Test-RunInputs) -notmatch 'SRT') { throw 'picture accepted SRT' }
$PictureFeatureButton.IsChecked=$false; Sync-FeatureMode
if ((Get-SelectedTaskMode) -ne 'srt_translation' -or (Test-RunInputs)) { throw 'valid SRT blocked' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root))
            frontend.FrontendPresentationTests().run_ui(root, body)
