from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys

from full_workflow.contracts import FullWorkflowRequest
from translation_flow.events import EventEmitter
from translation_flow.task_queue import request_info
from translation_flow.cancellation import TranslationCancelled
from video_enhancement.jobs import SRRequest, SRRunner, TranslationSRRunner, sr_options
from video_enhancement.runtime import MODEL, NOMOS_MODEL, check_runtime, model_spec, required_files
from video_enhancement import workflow as sr_workflow
from video_enhancement.workflow import SRConfig, identity, probe, read_json, run_command, run_super_resolution, write_json
from tests.test_full_workflow import _request, _FakeTranslationRunner, _FakeFinalMux

TOOLS = Path(os.environ.get('SR_TEST_TOOL_ROOT', 'missing-tools'))
FFMPEG = TOOLS / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
FFPROBE = FFMPEG.with_name('ffprobe.exe')


class SuperResolutionContracts(unittest.TestCase):
    def test_nomos_is_explicit_standalone_and_legacy_options_stay_identical(self):
        self.assertEqual(sr_options(None), {'max_height': 1080})
        self.assertEqual(sr_options({'model': MODEL}), {'max_height': 1080})
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'neutral.mp4'
            source.write_bytes(b'fixture')
            payload = dict(schema_version=1, workflow_mode='super_resolution', job_id='sr-nomos',
                           input_video=str(source), output_dir=str(root), output_target='mkv',
                           super_resolution={'max_height': 720, 'model': NOMOS_MODEL})
            request = SRRequest.from_mapping(payload)
            self.assertEqual(request.options, payload['super_resolution'])
            path = root / '.super-resolution/sr-nomos/request.json'
            write_json(path, payload)
            self.assertEqual(request_info(path)['provider'], '')
            with self.assertRaisesRegex(ValueError, '单独超分'):
                FullWorkflowRequest.from_mapping({**payload, 'workflow_mode': 'translation_super_resolution'})
            for model in ('unknown', None, []):
                with self.assertRaises(ValueError):
                    sr_options({'model': model}, standalone=True)

    def test_native_model_routes_use_original_weights_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for model in (MODEL, NOMOS_MODEL):
                for path in required_files(root, model):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'fixture')
                config = SRConfig(root / 'source.mkv', root, root / 'out.mkv', FFMPEG, FFPROBE,
                                  runtime=root, model=model)
                with patch.object(sr_workflow, 'run_command') as run:
                    sr_workflow.native_upscale(root / 'in', root / 'out', {'width': 1284, 'height': 720}, config, lambda: False)
                args = [str(arg) for arg in run.call_args.args[0]]
                self.assertEqual(Path(args[0]).name, model_spec(model)['executable'])
                self.assertEqual(args[args.index('-n') + 1], model_spec(model)['weights'])
                if model == NOMOS_MODEL:
                    self.assertEqual(args[args.index('-s') + 1], '2')
                    self.assertEqual(args[args.index('-j') + 1], '4:4:4')
                    self.assertNotIn('-r', args)
                    required_files(root, model)[0].unlink()
                    with self.assertRaisesRegex(FileNotFoundError, 'NomosUni'):
                        check_runtime(root, model)
                else:
                    self.assertEqual(args[args.index('-r') + 1], '1284x720')
                    self.assertNotIn('-s', args)

    def test_local_request_and_queue_need_no_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'neutral.mp4'
            source.write_bytes(b'fixture')
            payload = dict(schema_version=1, workflow_mode='super_resolution', job_id='sr-test',
                           input_video=str(source), output_dir=str(root), output_target='mkv')
            request = SRRequest.from_mapping(payload)
            self.assertEqual(request.options, {'max_height': 1080})
            path = root / '.super-resolution/sr-test/request.json'
            write_json(path, payload)
            info = request_info(path)
            self.assertEqual(info['module'], 'video_enhancement.cli')
            self.assertEqual(info['provider'], '')
            for change in ({'provider': {}}, {'preview_start_seconds': -1},
                           {'super_resolution': {'max_height': 999}}, {'preview_start_seconds': float('nan')}):
                with self.assertRaises(ValueError):
                    SRRequest.from_mapping({**payload, **change})

    def test_translation_join_waits_for_asr_and_reuses_translation_after_sr_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = _request(root)
            request = replace(base, workflow_mode='translation_super_resolution', super_resolution={'max_height': 1080},
                              translation=replace(base.translation, profile_id='normal'))
            calls, configs, failures = [], [], [True]
            def translation(*a, **kw):
                return _FakeTranslationRunner(*a, calls=calls, **kw)
            def enhance(config, **kw):
                self.assertIn('translation', calls)
                if failures[0]:
                    failures[0] = False
                    raise RuntimeError('simulated SR error')
                config.work_dir.mkdir(parents=True, exist_ok=True)
                config.output.write_bytes(b'video')
                report = dict(status='technical_checks_passed', scope='full', output=str(config.output),
                              source_identity=identity(config.source), source_origin_seconds=0,
                              segment_mappings=[dict(index=0, source_start_seconds=0, source_end_seconds=5,
                                                     output_start_seconds=0, output_end_seconds=5)],
                              output_seam_positions_seconds=[], final_duration_seconds=5, route={'fixture': True},
                              report=str(config.work_dir / 'report.json'))
                write_json(config.work_dir / 'report.json', report)
                kw['on_progress'](120)
                return report
            runner = TranslationSRRunner(request, 'offline-fixture', sr_runner=enhance,
                translation_runner_factory=translation,
                final_mux_factory=lambda *a: _FakeFinalMux(*a, calls=calls, configs=configs),
                emitter=EventEmitter(request.translation.job_id, root / 'events.jsonl', StringIO()))
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                runner.run()
            result = runner.run()
            self.assertEqual(calls.count('translation'), 1)
            self.assertIn('.upscaled.', result['outputs']['final_mkv'])
            self.assertEqual(result['technical_validation']['status'], 'passed')
            self.assertEqual(read_json(runner.paths.manifest)['workflow_mode'], 'translation_super_resolution')


@unittest.skipUnless(FFMPEG.is_file(), 'set SR_TEST_TOOL_ROOT to existing FFmpeg tools')
class SuperResolutionCPU(unittest.TestCase):
    def test_nomos_native_handoff_stop_resume_and_model_isolation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self.make_source(root, rate='24', duration=12)
            before = identity(source)
            runtime = root / 'runtime'
            for path in required_files(runtime, NOMOS_MODEL):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fake native engine')
            config = SRConfig(source, root / 'work', root / 'nomos.mkv', FFMPEG, FFPROBE,
                              max_height=60, runtime=runtime, model=NOMOS_MODEL)
            batches, stop = [], [False]
            def transport(args, cancelled, **kwargs):
                if Path(args[0]).name == 'span-ncnn-vulkan.exe':
                    inputs, outputs = Path(args[args.index('-i') + 1]), Path(args[args.index('-o') + 1])
                    batches.append(len(list(inputs.glob('*.png'))))
                    # Only native inference is substituted; model output is 2x,
                    # deliberately larger than the final target in this test.
                    return run_command([FFMPEG, '-v', 'error', '-n', '-i', inputs / '%08d.png',
                                        '-vf', 'scale=iw*2:ih*2', '-start_number', '0', outputs / '%08d.png'], cancelled)
                return run_command(args, cancelled, **kwargs)
            def progress(frames):
                if frames >= 256:
                    stop[0] = True
            with patch.object(sr_workflow, 'run_command', side_effect=transport):
                with self.assertRaises(TranslationCancelled):
                    run_super_resolution(config, cancelled=lambda: stop[0], on_progress=progress)
                saved = identity(config.work_dir / 'chunk-000000.mkv')
                self.assertEqual(batches, [256])
                self.assertFalse(list(config.work_dir.glob('frames-*')))
                with self.assertRaisesRegex(ValueError, '选项'):
                    run_super_resolution(replace(config, model=MODEL))
                counts = []
                result = run_super_resolution(config, on_progress=counts.append)
                self.assertEqual(counts[0], 256)
                self.assertEqual(batches, [256, 32])
                self.assertEqual(identity(config.work_dir / 'chunk-000000.mkv'), saved)
                self.assertEqual(result['frames'], 288)
                self.assertEqual((result['route']['width'], result['route']['height']), (106, 60))
                self.assertEqual(result['route']['model'], model_spec(NOMOS_MODEL)['weights'])
                self.assertTrue(run_super_resolution(config)['reused'])
            self.assertEqual(identity(source), before)

    def test_local_cli_publishes_library_result_without_gpu_or_service(self):
        from translation_flow.library import list_deliveries
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'neutral-720p.mkv'
            run_command([FFMPEG, '-v', 'error', '-n', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=10:duration=0.5',
                         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5', '-c:v', 'libx264',
                         '-preset', 'ultrafast', '-c:a', 'aac', source], lambda: False)
            out = root / 'out'
            request = out / '.super-resolution/sr-cli/request.json'
            write_json(request, dict(schema_version=1, workflow_mode='super_resolution', job_id='sr-cli',
                       input_video=str(source), output_dir=str(out), output_target='mkv', super_resolution={'max_height': 720}))
            # Force a nonexistent SR runtime: the at-target path must be local
            # stream-copy and must never try a GPU/model or translation service.
            proc = subprocess.run([sys.executable, '-B', '-m', 'video_enhancement.cli', '--request', str(request),
                                   '--tool-root', str(TOOLS)], capture_output=True, text=True, encoding='utf-8',
                                   env={**os.environ, 'TRANSLATION_FLOW_SR_ROOT': str(root / 'no-runtime')}, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(events[-1]['event'], 'job_completed')
            rows = list_deliveries(out)['items']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['workflow_mode'], 'super_resolution')
            self.assertTrue(Path(rows[0]['media']).is_file())

    def make_source(self, root, *, rate='24000/1001', duration=6):
        source = root / 'neutral.mkv'
        run_command([FFMPEG, '-v', 'error', '-n', '-f', 'lavfi', '-i', f'testsrc2=size=64x36:rate={rate}:duration={duration}',
                     '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-c:v', 'libx264',
                     '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', source], lambda: False)
        return source

    @staticmethod
    def fake_model(inputs, outputs, plan, config, cancelled):
        # A deterministic CPU resize substitutes ONLY for model inference. This
        # tests the real decode/checkpoint/encode/mux code, not neural quality.
        run_command([FFMPEG, '-v', 'error', '-n', '-i', inputs / '%08d.png',
                     '-vf', f"scale={plan['width']}:{plan['height']}", '-start_number', '0', outputs / '%08d.png'], cancelled)

    def test_fractional_fps_stop_resume_and_bounded_scratch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self.make_source(root)
            before = identity(source)
            config = SRConfig(source, root / 'work', root / 'processed.mkv', FFMPEG, FFPROBE)
            stop = [False]
            def progress(frames):
                if frames >= 64:
                    stop[0] = True
            with self.assertRaises(TranslationCancelled):
                run_super_resolution(config, upscale=self.fake_model, cancelled=lambda: stop[0], on_progress=progress)
            saved = identity(root / 'work/chunk-000000.mkv')
            self.assertFalse(list((root / 'work').glob('frames-*')))
            counts = []
            result = run_super_resolution(config, upscale=self.fake_model, on_progress=counts.append)
            self.assertEqual(counts[0], 64)
            self.assertEqual(identity(root / 'work/chunk-000000.mkv'), saved)
            self.assertEqual(identity(source), before)
            self.assertEqual((result['route']['width'], result['route']['height']), (128, 72))
            self.assertLess(abs(result['final_duration_seconds'] - 6), 0.1)
            self.assertTrue(run_super_resolution(config, upscale=self.fake_model)['reused'])
            with self.assertRaisesRegex(ValueError, '选项'):
                run_super_resolution(replace(config, max_height=720), upscale=self.fake_model)

    def test_local_mux_and_independent_preview(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self.make_source(root, duration=3)
            payload = dict(schema_version=1, workflow_mode='super_resolution', job_id='sr-local',
                           input_video=str(source), output_dir=str(root / 'out'), output_target='mkv',
                           super_resolution={'model': NOMOS_MODEL})
            def sr(config, **kw):
                return run_super_resolution(config, upscale=self.fake_model, **kw)
            outputs = []
            for job, start in [('sr-local', None), ('sr-preview', 1)]:
                value = {**payload, 'job_id': job}
                if start is not None:
                    value['preview_start_seconds'] = start
                request = SRRequest.from_mapping(value)
                record = request.output_dir / '.super-resolution' / job
                write_json(record / 'request.json', value)
                result = SRRunner(request, tools=TOOLS, sr_runner=sr,
                    emitter=EventEmitter(job, record / 'events.jsonl', StringIO())).run()
                self.assertEqual(result['super_resolution']['model'], NOMOS_MODEL)
                media = Path(result['outputs']['final_media'])
                actual = probe(FFPROBE, media, lambda: False)
                self.assertEqual(sum(s['codec_type'] == 'audio' for s in actual['streams']), 1)
                outputs.append(media)
                if start is not None:
                    self.assertTrue(Path(result['outputs']['comparison']).is_file())
                    self.assertLess(float(actual['format']['duration']), 2.1)
            self.assertNotEqual(*outputs)
            self.assertTrue(all(p.is_file() for p in outputs))

    def test_already_at_target_copies_video_without_model(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self.make_source(root, duration=1)
            config = SRConfig(source, root / 'copy', root / 'original.video-only.mkv', FFMPEG, FFPROBE, max_height=36)
            def unused(*args):
                self.fail('no enlargement must not call the model')
            report = run_super_resolution(config, upscale=unused)
            self.assertEqual(report['route']['engine'], 'video_copy')
            self.assertLess(abs(report['final_duration_seconds'] - 1), 0.1)
            self.assertFalse(list(config.work_dir.glob('chunk-*.mkv')))

    def test_delayed_video_keeps_its_first_frames_and_source_origin(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'delayed.mkv'
            run_command([FFMPEG, '-v', 'error', '-n', '-f', 'lavfi', '-i', 'testsrc2=size=64x36:rate=24:duration=2',
                         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
                         '-vf', 'setpts=PTS+2/TB', '-fps_mode', 'passthrough', '-c:v', 'libx264',
                         '-c:a', 'aac', source], lambda: False)
            for name, height in [('copy', 36), ('sr', 1080)]:
                config = SRConfig(source, root / name, root / (name + '.mkv'), FFMPEG, FFPROBE, max_height=height)
                report = run_super_resolution(config, upscale=self.fake_model)
                self.assertAlmostEqual(report['source_origin_seconds'], 2, delta=0.05)
                self.assertAlmostEqual(report['final_duration_seconds'], 2, delta=0.05)
                if name == 'sr':
                    self.assertEqual(report['frames'], 48)


@unittest.skipUnless(os.name == 'nt', 'native Windows desktop')
class SuperResolutionUI(unittest.TestCase):
    def test_native_options_identity_preview_and_resume(self):
        from tests.test_frontend_presentation import FrontendPresentationTests
        ui = FrontendPresentationTests()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'neutral.mp4').write_bytes(b'offline fixture')
            with ui.isolated_queue(root) as (project, state, start, worker):
                ui.run_ui(root, r'''
if ($null -eq $script:QueueSnapshot) { return }
if ($SuperResolutionCheckBox.IsChecked -or -not $UseCCheckBox.IsChecked) { throw 'defaults changed' }
if ($SuperResolutionModelBox.SelectedItem.Tag -ne 'upscayl-lite-4x') { throw 'default model changed' }
Set-SelectedFiles @('TESTROOT\neutral.mp4')
$OutputDirectoryBox.Text='TESTROOT\out'
$SuperResolutionCheckBox.IsChecked=$true
$SuperResolutionCheckBox.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Primitives.ButtonBase]::ClickEvent))
if ((Get-SelectedTaskMode) -ne 'translation_super_resolution') { throw 'combined mode missing' }
$script:CollectQueueRequests=$true
Start-ConfirmedRun
$combinedPath=$script:CollectedQueueItems[0].request_path
$combined=Get-Content -LiteralPath $combinedPath -Raw | ConvertFrom-Json
if ($combined.super_resolution.max_height -ne 1080 -or $combined.workflow_mode -ne 'translation_super_resolution') { throw 'combined request lost options' }
$script:CollectedQueueItems.Clear()
$SubtitleFeatureButton.IsChecked=$false
Sync-FeatureMode
if ((Get-SelectedTaskMode) -ne 'super_resolution' -or $ProviderPanel.Visibility -ne 'Collapsed') { throw 'local mode still requires translation' }
Start-ConfirmedRun
$localPath=$script:CollectedQueueItems[0].request_path
$local=Get-Content -LiteralPath $localPath -Raw | ConvertFrom-Json
$legacyRaw=Get-Content -LiteralPath $localPath -Raw
if ($null -ne $local.PSObject.Properties['provider'] -or $null -ne $local.PSObject.Properties['consent']) { throw 'local request has provider' }
if ($local.job_id -eq $combined.job_id) { throw 'identity collision' }
$resume=Get-TaskCenterResumeRequest $localPath
if ($resume.TaskMode -ne 'super_resolution' -or $resume.SuperResolutionHeight -ne 1080) { throw 'resume lost settings' }
if ($resume.SuperResolutionModel -ne 'upscayl-lite-4x') { throw 'legacy model lost' }
$script:CollectedQueueItems.Clear()
Select-ComboBoxItemByTag $SuperResolutionModelBox 'nomosuni-otf-medium'
if ($SuperResolutionModelPanel.Visibility -ne 'Visible' -or $TaskModeHintText.Text -notmatch 'NomosUni') { throw 'standalone model label missing' }
if ($SubtitleFeatureButton.IsEnabled) { throw 'standalone candidate allows accidental translation switch' }
Start-ConfirmedRun
$nomosPath=$script:CollectedQueueItems[0].request_path
$nomos=Get-Content -LiteralPath $nomosPath -Raw | ConvertFrom-Json
if ($nomos.super_resolution.model -ne 'nomosuni-otf-medium' -or $nomos.job_id -eq $local.job_id) { throw 'model identity collision' }
if ($legacyRaw -cne (Get-Content -LiteralPath $localPath -Raw)) { throw 'legacy request changed' }
$resume=Get-TaskCenterResumeRequest $nomosPath
if ($resume.SuperResolutionModel -ne 'nomosuni-otf-medium') { throw 'model resume lost settings' }
$originalSnapshot=$script:QueueSnapshot
$script:QueueSnapshot=[pscustomobject]@{items=@([pscustomobject]@{id='model-fixture';request_path=$nomosPath})}
$script:PendingQueueResumeId='model-fixture'
if (Test-QueueResumeSettings) { throw 'matching model cannot resume' }
Select-ComboBoxItemByTag $SuperResolutionModelBox 'upscayl-lite-4x'
if (-not $SubtitleFeatureButton.IsEnabled) { throw 'legacy Lite options were disabled' }
if (-not (Test-QueueResumeSettings)) { throw 'changed model allowed to overwrite old task' }
Select-ComboBoxItemByTag $SuperResolutionModelBox 'nomosuni-otf-medium'
$script:PendingQueueResumeId=''
$script:QueueSnapshot=$originalSnapshot
$script:CollectQueueRequests=$false
$script:CapturedPreview=$null
function Start-QueueCall($Payload) { $script:CapturedPreview=$Payload }
$SuperResolutionPreviewStartBox.Text='12.5'
$SuperResolutionPreviewButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($null -eq $script:CapturedPreview) { throw ('preview missing: '+$InputErrorText.Text) }
$previewPath=$script:CapturedPreview.items[0].request_path
$preview=Get-Content -LiteralPath $previewPath -Raw | ConvertFrom-Json
if ($preview.preview_start_seconds -ne 12.5 -or $preview.job_id -eq $local.job_id) { throw 'preview identity/window wrong' }
if ($preview.super_resolution.model -ne 'nomosuni-otf-medium') { throw 'preview lost model' }
$resume=Get-TaskCenterResumeRequest $previewPath
if ($resume.PreviewStart -ne 12.5) { throw 'preview resume lost start' }
if ($null -ne $script:SRPreviewStart) { throw 'preview changed next full task' }
$script:QueueSubmitting=$false
$script:CurrentWorkflowMode='translation_super_resolution'
Reset-RunPresentation
$script:IsRunning=$true
Update-LivePresentation ([pscustomobject]@{event='stage_progress';stage='super_resolution';processed_frames=128})
Update-LivePresentation ([pscustomobject]@{event='stage_progress';stage='polish';overall_percent=80})
Update-ProgressPresentation
if ($ProgressText.Text -notmatch '128' -or $ProgressBar.Visibility -ne 'Hidden') { throw 'translation erased SR progress' }
Update-LivePresentation ([pscustomobject]@{event='stage_completed';stage='super_resolution'})
Update-ProgressPresentation
if ($ProgressText.Text -match '128') { throw 'completed SR still looks running' }
$script:IsRunning=$false
$NewTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
if ($SuperResolutionCheckBox.IsChecked -or -not $UseCCheckBox.IsChecked) { throw 'new task defaults changed' }
if ($SuperResolutionModelBox.SelectedItem.Tag -ne 'upscayl-lite-4x') { throw 'new task changed default model' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root)), project=project, state=state)


if __name__ == '__main__':
    unittest.main()
