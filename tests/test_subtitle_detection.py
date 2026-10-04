import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from translation_flow.subtitle_detection import chinese_dialogue, inspect_subtitles, subtitle_lines
from full_workflow.picture import preserve_sidecars


class SubtitleDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'sample.mp4'
        self.source.write_bytes(b'not real media; probe and OCR injected')

    def probe(self, subtitles=()):
        return json.dumps({'format': {'duration': 100}, 'streams': [
            {'codec_type': 'video', 'index': 0}, *subtitles]}).encode()

    def test_chinese_dialogue_is_not_any_han_text(self):
        for value in ('我 们 今 天 一 起 去 看 电 影 吧', '你們這是在做什麼', '不要这样看着我'):
            self.assertTrue(chinese_dialogue(value), value)
        for value in ('東京大学', '私は映画を見ます', '中文字幕组制作', '广告不要错过', 'English subtitles'):
            self.assertFalse(chinese_dialogue(value), value)

    def test_exact_sidecar_content_short_circuits_all_media_tools(self):
        text = '1\n00:00:01,000 --> 00:00:02,000\n我们今天一起去看电影吧\n'
        (self.root / 'sample2.srt').write_text(text, encoding='utf-8')
        (self.root / 'sample.zh.srt').write_text(text, encoding='utf-8')
        def no_tools(*args, **kwargs):
            raise AssertionError('sidecar should avoid tools')
        result = inspect_subtitles(self.source, run=no_tools, ocr=no_tools)
        self.assertEqual('sidecar', result['kind'])
        self.assertTrue(result['evidence'][0]['path'].endswith('sample.zh.srt'))

    def test_tagged_chinese_track_short_circuits_ocr(self):
        def run(args, **kwargs):
            self.assertIn('-show_streams', args)
            return self.probe([{'codec_type': 'subtitle', 'index': 3, 'tags': {'language': 'zho'}}])
        result = inspect_subtitles(self.source, run=run, ocr=lambda _: self.fail('no OCR'))
        self.assertEqual(('detected', 'embedded'), (result['status'], result['kind']))

    def test_unlabelled_text_track_reads_content(self):
        def run(args, **kwargs):
            if '-show_streams' in args:
                return self.probe([{'codec_type': 'subtitle', 'codec_name': 'subrip', 'index': 2}])
            self.assertIn('0:2', args)
            return '1\n00:00:01,000 --> 00:00:02,000\n这个地方我们来过\n'.encode()
        self.assertEqual('embedded', inspect_subtitles(self.source, run=run)['kind'])

    def test_one_burned_dialogue_line_stops_later_batches_and_watermarks_miss(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return self.probe() if '-show_streams' in args else b''
        def single_line(paths):
            return [{'lines': [{'text': text}]} for text in ('我们今天一起出去吧', '', '', '')]
        result = inspect_subtitles(self.source, run=run, ocr=single_line)
        self.assertEqual('burned', result['kind'])
        self.assertEqual(1, len(result['evidence']))
        self.assertEqual(5, len(calls))  # probe plus first four samples, not whole film
        calls.clear()
        self.assertEqual('not_detected', inspect_subtitles(self.source, run=run, ocr=lambda paths: [
            {'lines': [{'text': '中文字幕组制作'}]} for _ in paths])['status'])
        self.assertEqual(13, len(calls))

    def test_missing_ocr_is_unknown_not_absent_and_srt_is_explicit_input(self):
        def fail(_):
            raise RuntimeError('no Chinese OCR')
        result = inspect_subtitles(self.source, run=lambda *a, **k: self.probe(), ocr=fail)
        self.assertEqual('unknown', result['status'])
        self.assertIn('no Chinese OCR', result['warnings'])
        srt = self.root / 'document.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n我们在这里\n', encoding='utf-8')
        self.assertEqual('not_applicable', inspect_subtitles(srt)['status'])

    def test_ass_only_reads_dialogue_and_preserves_sidecar_without_overwrite(self):
        content = '[Script Info]\nTitle: 我们在这里\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你们在做什么\n'
        self.assertEqual(['你们在做什么'], subtitle_lines(content, '.ass'))
        subtitle = self.root / 'sample.zh.ass'
        subtitle.write_text(content, encoding='utf-8')
        output = self.root / 'result.restored.mkv'
        paths = preserve_sidecars(self.source, output)
        self.assertEqual(content, Path(paths[0]).read_text(encoding='utf-8'))
        self.assertEqual(paths, preserve_sidecars(self.source, output))
        Path(paths[0]).write_text('existing result', encoding='utf-8')
        with self.assertRaises(ValueError):
            preserve_sidecars(self.source, output)
        self.assertEqual('existing result', Path(paths[0]).read_text(encoding='utf-8'))


@unittest.skipUnless(os.name == 'nt', 'WPF is Windows-only')
class SubtitleImportUiTests(unittest.TestCase):
    def test_mixed_batch_skip_override_and_picture_routes(self):
        import test_frontend_presentation as frontend
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('with.mp4', 'without.mp4'):
                (root / name).write_bytes(b'path-only UI fixture')
            body = r'''
function Start-QueueCall($Payload) { $script:TestPayload=$Payload; return $true }
$script:QueueEnabled=$true
$script:QueueTimer=New-Object Windows.Threading.DispatcherTimer
$OutputDirectoryBox.Text='TESTROOT\out'
Set-SelectedFiles @('TESTROOT\with.mp4','TESTROOT\without.mp4')
$script:ImportPaths=@($script:SelectedFiles)
foreach ($path in $script:ImportPaths) {
    $status=$(if ($path -like '*\with.mp4') { 'detected' } else { 'not_detected' })
    $script:ImportReports[$path]=@{Identity=(Get-ImportIdentity $path);Report=[pscustomobject]@{status=$status;message=$status;evidence=@();warnings=@()}}
}
Update-SubtitleImportSummary
Start-AuthorizedBatch
if ($script:TestPayload.items.Count -ne 1) { throw 'subtitled file was not skipped' }
$request=Get-Content -LiteralPath $script:TestPayload.items[0].request_path -Raw | ConvertFrom-Json
if ($request.input_video -notlike '*\without.mp4') { throw 'wrong video translated' }
$script:QueueSubmitting=$false
$script:SelectedFiles=@('TESTROOT\with.mp4','TESTROOT\without.mp4')
$SubtitleFeatureButton.IsChecked=$true; $PictureFeatureButton.IsChecked=$true; Sync-FeatureMode
Start-AuthorizedBatch
$requests=@($script:TestPayload.items | ForEach-Object { Get-Content -LiteralPath $_.request_path -Raw | ConvertFrom-Json })
if ($requests.Count -ne 2 -or $requests[0].workflow_mode -ne 'picture_only' -or $requests[1].workflow_mode -ne 'translation_picture_final') { throw 'mixed dual routes incorrect' }
if ($requests[0].PSObject.Properties['provider']) { throw 'skipped translation still has provider' }
$script:QueueSubmitting=$false
$script:ImportPaths=@('TESTROOT\with.mp4'); $script:SelectedFiles=@($script:ImportPaths)
$InputVideoBox.Text=$script:ImportPaths[0]
$PictureFeatureButton.IsChecked=$false; $SubtitleFeatureButton.IsChecked=$true; Sync-FeatureMode
Update-SubtitleImportSummary
if ((Get-SelectedTaskMode) -ne 'none' -or -not $script:ImportAutoDeselected) { throw 'all detected did not auto-uncheck translation' }
function Start-CheckedBatch { throw 'no work must not check provider or run backend' }
Begin-ConfiguredRun
if ($InputErrorText.Text -notmatch '无需处理') { throw 'no-work state not shown' }
$SubtitleFeatureButton.IsChecked=$true
$SubtitleFeatureButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
if (-not $ForceSubtitleTranslationBox.IsChecked -or (Get-ImportedTaskMode $InputVideoBox.Text 'translation') -ne 'translation') { throw 'manual recheck did not override detection' }
[IO.File]::AppendAllText($InputVideoBox.Text,'changed')
if ($null -ne (Get-SubtitleImportReport $InputVideoBox.Text)) { throw 'changed source reused old detection' }
$script:TestResult=@{status='passed'}
'''.replace('TESTROOT', str(root))
            frontend.FrontendPresentationTests().run_ui(root, body)


@unittest.skipUnless(os.environ.get('SUBTITLE_DETECTION_TOOL_ROOT'), 'set tool root for real synthetic media/OCR')
class SubtitleDetectionSyntheticTests(unittest.TestCase):
    def test_real_soft_hard_and_plain_media(self):
        from PIL import Image, ImageDraw, ImageFont
        tools = Path(os.environ['SUBTITLE_DETECTION_TOOL_ROOT'])
        ffmpeg = tools / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 40)
            for i, text in enumerate(('我们今天一起去看电影吧', '这个地方我们以前来过', '')):
                im = Image.new('RGB', (960, 540), (30, 40, 50))
                ImageDraw.Draw(im).text((210, 440), text, font=font, fill='white')
                im.save(root / f'{i}.png')
            def ff(args):
                subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-n', *map(str,args)], check=True,
                               capture_output=True, timeout=30)
            hard = root / 'hard.mp4'
            ff(['-framerate','1/3','-i',root/'%d.png','-t','6','-r','24','-c:v','libx264','-pix_fmt','yuv420p',hard])
            hard_report = inspect_subtitles(hard, tools=tools)
            self.assertEqual('burned', hard_report['kind'], hard_report)
            plain = root / 'plain.mp4'
            ff(['-loop','1','-i',root/'2.png','-t','3','-c:v','libx264','-pix_fmt','yuv420p',plain])
            self.assertEqual('not_detected', inspect_subtitles(plain, tools=tools)['status'])
            srt = root / 'source.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:02,000\n我们今天一起去看电影吧\n', encoding='utf-8')
            soft = root / 'soft.mkv'
            ff(['-i',plain,'-i',srt,'-map','0:v','-map','1:s','-c','copy','-metadata:s:s:0','language=zho',soft])
            self.assertEqual('embedded', inspect_subtitles(soft, tools=tools)['kind'])

    def test_real_async_import_reaches_wpf_without_service(self):
        import test_frontend_presentation as frontend
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # External subtitle detection uses real local content, no media decoder.
            (root/'video.mp4').write_bytes(b'path fixture; no video decoding expected')
            (root/'video.zh.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n我们今天一起去看电影吧\n', encoding='utf-8')
            body = r'''
if ($script:TestStep -eq 0) {
    function Start-CheckedBatch { throw 'already-subtitled input must not check service' }
    $OutputDirectoryBox.Text='TESTROOT\out'
    Set-SelectedFiles @('TESTROOT\video.mp4')
    $script:TestStep=1
    return
}
if ($null -ne $script:ImportBridge) { return }
$report=Get-SubtitleImportReport 'TESTROOT\video.mp4'
if ($null -eq $report) { return }
if ($report.status -ne 'detected' -or $report.kind -ne 'sidecar') { throw ('detection failed: '+($report|ConvertTo-Json -Compress)) }
if ((Get-SelectedTaskMode) -ne 'none') { throw 'asynchronous import did not uncheck translation' }
Begin-ConfiguredRun
if ($InputErrorText.Text -notmatch '无需处理') { throw 'unexpected no-work action' }
$script:TestResult=@{status='passed';message=$SubtitleImportText.Text}
'''.replace('TESTROOT', str(root))
            try:
                frontend.FrontendPresentationTests().run_ui(root, body, project=frontend.ROOT,
                                                            state=root/'queue', mock_import=False)
            finally:
                import time
                from translation_flow.queue_cli import rpc
                rpc(root/'queue', {'action': 'shutdown'})
                time.sleep(0.7)


if __name__ == '__main__':
    unittest.main()
