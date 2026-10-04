import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from translation_flow import library
from translation_flow.srt import parse_srt, write_srt, SubtitleCue
from translation_flow.task_queue import FileLease


def fixture(root, folder='.full-workflow', job='example'):
    path = root / folder / job / 'request.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    source = root / 'source.mkv'; source.write_bytes(b'original')
    request = dict(job_id=job, input_video=str(source), output_dir=str(root))
    path.write_text(json.dumps(request), encoding='utf-8')
    media = root / f'{job}.final.mkv'; media.write_bytes(b'final')
    chinese = root / f'{job}.zh.srt'
    write_srt(chinese, [SubtitleCue(1, 0, 900, '你好'), SubtitleCue(2, 1200, 2900, '明天见')])
    japanese = root / '.translation-flow' / job / 'asr/fusion/fusion.ja.srt'
    if folder != '.full-workflow': japanese = path.parent / 'asr/fusion/fusion.ja.srt'
    write_srt(japanese, [SubtitleCue(1,0,900,'こんにちは'), SubtitleCue(2,1200,2900,'また明日')])
    report = path.parent / 'mux.json'
    report.write_text(json.dumps(dict(source_start_ms=100, processed_duration_ms=2800)), encoding='utf-8')
    outputs = dict(final_mkv=str(media), chinese_srt=str(chinese), technical_report=str(report))
    picture = path.parent / 'picture'
    segment = picture / 'segments_input/segment_0000.mkv'
    segment.parent.mkdir(parents=True); segment.write_bytes(b'segment')
    pure = root / f'{job}.video-only.mkv'; pure.write_bytes(b'pure video')
    outputs['processed_video'] = str(pure)
    picture_report = picture / 'reports/workflow-report.json'; picture_report.parent.mkdir()
    picture_report.write_text(json.dumps(dict(status='technical_checks_passed',output=str(pure))))
    result = dict(status='technical_checks_passed',technical_validation=dict(status='passed'), outputs=outputs)
    (path.parent / library.RESULTS[folder]).write_text(json.dumps(result), encoding='utf-8')
    (path.parent / 'events.jsonl').write_text('{"event":"job_completed"}\n',encoding='utf-8')
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.request=fixture(self.root)

    def test_catalog_excludes_pure_video_and_collapses_full_translation(self):
        child=self.root/'.translation-flow/example'
        child.mkdir(parents=True, exist_ok=True)
        (child/'request.json').write_bytes(self.request.read_bytes())
        (child/'job.result.json').write_bytes((self.request.parent/'workflow.result.json').read_bytes())
        catalog=library.list_deliveries(self.root)
        self.assertEqual(len(catalog['items']),1)
        self.assertEqual(catalog['items'][0]['variants'],[str(self.root/'example.final.mkv')])

    def test_cleanup_requires_confirmation_and_retains_all_evidence(self):
        plan=library.cleanup_plan(self.request)
        self.assertEqual(plan['count'],2)
        with self.assertRaises(ValueError): library.clean_checkpoints(plan)
        before=self.request.read_bytes()
        report=(self.request.parent/'workflow.result.json').read_bytes()
        result=library.clean_checkpoints(plan,confirmed=True)
        self.assertEqual(result['deleted_count'],2)
        self.assertTrue((self.root/'source.mkv').exists())
        self.assertTrue((self.root/'example.final.mkv').exists())
        self.assertTrue((self.root/'example.zh.srt').exists())
        self.assertEqual(self.request.read_bytes(),before)
        self.assertEqual((self.request.parent/'workflow.result.json').read_bytes(),report)
        self.assertEqual(library.list_deliveries(self.root)['items'][0]['cleaned'],True)

    def test_changed_plan_rejects_before_any_delete(self):
        plan=library.cleanup_plan(self.request)
        Path(plan['files'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError): library.clean_checkpoints(plan,confirmed=True)
        self.assertTrue(all(Path(p['path']).exists() for p in plan['files']))

    def test_missing_retained_movie_or_active_event_blocks(self):
        plan=library.cleanup_plan(self.request)
        (self.request.parent/'events.jsonl').write_text('{"event":"job_started"}\n')
        with self.assertRaises(ValueError): library.clean_checkpoints(plan,confirmed=True)
        self.assertTrue(all(Path(p['path']).exists() for p in plan['files']))

    def test_job_lease_blocks_cleanup(self):
        plan=library.cleanup_plan(self.request)
        with FileLease(self.request.parent/'queue.run.lock'):
            with self.assertRaises(OSError): library.clean_checkpoints(plan,confirmed=True)
        self.assertTrue(all(Path(p['path']).exists() for p in plan['files']))

    def test_outside_candidate_and_source_candidate_rejected(self):
        result_path=self.request.parent/'workflow.result.json'
        result=json.loads(result_path.read_text())
        result['outputs']['processed_video']=str(self.root/'source.mkv')
        result_path.write_text(json.dumps(result))
        with self.assertRaises(ValueError): library.cleanup_plan(self.request)

    def test_cleanup_failure_stops_and_marks_partial(self):
        plan=library.cleanup_plan(self.request)
        original=Path.unlink
        def denied(p,*a,**k):
            if str(p)==plan['files'][0]['path']: raise PermissionError('locked')
            return original(p,*a,**k)
        with patch.object(Path,'unlink',denied):
            with self.assertRaises(PermissionError): library.clean_checkpoints(plan,confirmed=True)
        record=library.read_json(self.request.parent/'retention-cleanup.json')
        self.assertEqual(record['status'],'partial')
        self.assertTrue(all(Path(p['path']).exists() for p in plan['files']))

    def test_picture_only_uses_nested_picture_report(self):
        request=fixture(self.root,'.picture-workflow','picture')
        self.assertEqual(library.cleanup_plan(request)['count'],2)

    def test_cleanup_never_targets_another_deliverable_from_wrong_report(self):
        report=self.request.parent/'picture/reports/workflow-report.json'
        unrelated=self.root/'other.final.mkv'; unrelated.write_bytes(b'keep')
        report.write_text(json.dumps(dict(status='technical_checks_passed',output=str(unrelated))))
        with self.assertRaises(ValueError): library.cleanup_plan(self.request)
        self.assertEqual(unrelated.read_bytes(),b'keep')

    def test_symlink_candidate_is_not_followed(self):
        target=self.root/'example.video-only.mkv'
        target.unlink()
        try: target.symlink_to(self.root/'source.mkv')
        except OSError: self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError): library.cleanup_plan(self.request)

    def test_manual_edit_preserves_originals_and_adjusts_playback_origin(self):
        review=library.load_review(self.request)
        self.assertEqual(review['rows'][0]['start_ms'],0)
        self.assertEqual(review['rows'][0]['end_ms'],800)
        self.assertEqual(review['rows'][1]['start_ms'],1100)
        self.assertEqual(review['rows'][0]['japanese'],'こんにちは')
        original=(self.root/'example.zh.srt').read_bytes()
        review['rows'][0]['chinese']='您好'
        saved=library.save_review(review)
        self.assertEqual(saved['changed_count'],1)
        self.assertEqual(parse_srt(saved['chinese_srt'])[0].text,'您好')
        self.assertEqual((self.root/'example.zh.srt').read_bytes(),original)
        self.assertNotEqual(library.save_review(review)['revision_path'],saved['revision_path'])

    def test_edit_rejects_empty_text_changed_ids_and_old_source(self):
        original=library.load_review(self.request)
        for change in ({'chinese':''},{'id':99},{'end_ms':7}):
            review=copy.deepcopy(original); review['rows'][0].update(change)
            with self.assertRaises(ValueError): library.save_review(review)
        (self.root/'example.zh.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nchanged\n')
        with self.assertRaises(ValueError): library.save_review(original)

    def test_json_cli_accepts_bom_and_unicode(self):
        p=subprocess.run([sys.executable,'-m','translation_flow.library'],input=('\ufeff'+json.dumps(dict(action='list',output_dir=str(self.root)))).encode(),capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(len(json.loads(p.stdout)['value']['items']),1)

    def test_ui_files_are_parsable_and_backend_runspace_returns(self):
        if os.name!='nt': self.skipTest('WPF requires Windows')
        root=Path(__file__).resolve().parents[1]
        payload=json.dumps(dict(action='list',output_dir=str(self.root)))
        # Exercise the exact detached scriptblock used by the WPF asynchronous button.
        script=f"""
        $ErrorActionPreference='Stop'
        Add-Type -AssemblyName PresentationFramework
        . '{root}/desktop/TranslationFlowLibrary.ps1'
        foreach($name in @('TranslationFlowLibrary.xaml','TranslationFlowReview.xaml')){{
          $w=New-LibraryWindow $name $null
          $w.Close()
        }}
        $s=[PowerShell]::Create()
        [void]$s.AddScript(${{function:Invoke-LibraryBackend}}.ToString()).AddArgument('{sys.executable}').AddArgument('{root}').AddArgument('{payload}')
        $v=@($s.Invoke())
        if($s.HadErrors){{throw ($s.Streams.Error|Out-String)}}
        if($v.Count -ne 1 -or @($v[0].items).Count -ne 1){{throw 'missing catalog result'}}
        $s.Dispose()
        """
        proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
        self.assertEqual(proc.returncode,0,proc.stderr)

    def test_native_buttons_load_edit_save_and_clean_only_fixture(self):
        if os.name!='nt': self.skipTest('WPF requires Windows')
        other_request=fixture(self.root,'.picture-workflow','other')
        root=Path(__file__).resolve().parents[1]
        script=f"""
        $ErrorActionPreference='Stop'
        Add-Type -AssemblyName PresentationFramework
        Add-Type -AssemblyName System.Windows.Forms
        . '{root}/desktop/TranslationFlowLibrary.ps1'
        $global:ProjectRoot='{root}'
        $global:PythonPathBox=New-Object Windows.Controls.TextBox
        $global:PythonPathBox.Text='{sys.executable}'
        $global:ToolRootBox=New-Object Windows.Controls.TextBox
        $global:Window=$null
        $global:Views=New-Object Collections.ArrayList
        $global:LibraryWindowFactory=${{function:New-LibraryWindow}}
        $global:Outcome=''
        $global:Step=0
        $global:BeautySource=''
        $global:CleanupAttempts=0
        function Show-BeautyPlayer([string]$Source) {{$global:BeautySource=$Source}}
        function Get-TaskCenterOutputDirectory {{return '{self.root}'}}
        function Refresh-TaskCenter {{}}
        function Show-CleanupConfirmation($Owner,$Plan) {{
          if($Plan.count -ne 2){{throw 'incorrect synthetic cleanup count'}}
          if(@($Plan.files|Where-Object{{$_.path -notlike '{self.root}*'}}).Count){{throw 'outside fixture'}}
          if($Plan.request_path -ne '{self.request}'){{throw 'card cleaned the previously selected product'}}
          $global:CleanupAttempts++
          return $global:CleanupAttempts -gt 1
        }}
        function New-LibraryWindow([string]$Name,$Owner) {{
          $view=& $global:LibraryWindowFactory $Name $Owner
          $view.Left=-20000;$view.Top=0;$view.WindowStartupLocation='Manual';$view.ShowInTaskbar=$false
          [void]$global:Views.Add($view)
          return $view
        }}
        $timer=New-Object Windows.Threading.DispatcherTimer
        $timer.Interval=[TimeSpan]::FromMilliseconds(150)
        $deadline=[DateTime]::UtcNow.AddSeconds(25)
        $timer.Add_Tick({{
          try {{
            if([DateTime]::UtcNow -gt $deadline) {{throw ('UI timeout step '+$global:Step+' '+($global:Views|ForEach-Object{{if($_.FindName('LibraryStatus')){{$_.FindName('LibraryStatus').Text;$_.FindName('LibraryStatus').ToolTip}};if($_.FindName('ReviewStatus')){{$_.FindName('ReviewStatus').Text;$_.FindName('ReviewStatus').ToolTip}}}}))}}
            if(-not $global:Views.Count){{return}}
            $lib=$global:Views[0]
            if($global:Step -eq 0 -and $lib.FindName('LibraryGrid').Items.Count){{
              $example=@($lib.FindName('LibraryGrid').Items|Where-Object{{$_.job_id -eq 'example'}})[0]
              $lib.FindName('LibraryGrid').SelectedItem=$example
              $global:CleanupButton=$lib.FindName('LibraryCards').Children[$lib.FindName('LibraryGrid').Items.IndexOf($example)].FindName('Cleanup')
              if($global:CleanupButton.Content -ne '清理中间文件' -or $global:CleanupButton.Visibility -ne 'Visible'){{throw 'missing visible per-product cleanup button'}}
              $lib.FindName('LibraryBeauty').RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
              if($global:BeautySource -ne '{self.root / 'example.final.mkv'}'){{throw ('beauty player did not receive selected final media: '+$global:BeautySource+' selected='+$lib.FindName('LibraryVariants').SelectedItem+' status='+$lib.FindName('LibraryStatus').Text+' trace='+$lib.FindName('LibraryStatus').ToolTip)}}
              $global:Step=1
              $lib.FindName('LibraryReview').RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
            }} elseif($global:Step -eq 1 -and $global:Views.Count -eq 2) {{
              $editor=$global:Views[1]
              $grid=$editor.FindName('ReviewGrid')
              if(-not $grid.Items.Count){{return}}
              $grid.Items[0].chinese='人工修订测试'
              $global:Step=2
              $editor.FindName('ReviewSave').RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
            }} elseif($global:Step -eq 2 -and $global:Views[1].FindName('ReviewStatus').Text -like '已另存*'){{
              $global:Views[1].Close();$global:Step=3
              $lib.FindName('LibraryGrid').SelectedItem=@($lib.FindName('LibraryGrid').Items|Where-Object{{$_.job_id -eq 'other'}})[0]
              $global:CleanupButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
            }} elseif($global:Step -eq 3 -and $lib.FindName('LibraryStatus').Text -like '已取消*'){{
              if(-not [IO.File]::Exists('{self.root / 'example.video-only.mkv'}')){{throw 'cancel deleted a checkpoint'}}
              $global:Step=4
              $global:CleanupButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
            }} elseif($global:Step -eq 4 -and $lib.FindName('LibraryStatus').Text -like '已永久删除*'){{
              if($lib.FindName('LibraryStatus').Text -notmatch 'GiB'){{throw 'cleanup size feedback missing'}}
              $global:Outcome='passed';$timer.Stop();$lib.Close()
            }}
          }} catch {{
            $global:Outcome=$_.Exception.Message;$timer.Stop()
            foreach($view in @($global:Views)){{$view.Close()}}
          }}
        }})
        $timer.Start()
        Show-DeliveryLibrary
        if($global:Outcome -ne 'passed'){{throw $global:Outcome}}
        """
        proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=40)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertTrue((self.root/'example.final.mkv').exists())
        self.assertFalse((self.root/'example.video-only.mkv').exists())
        self.assertTrue((self.root/'other.video-only.mkv').exists())
        self.assertEqual(library.cleanup_plan(other_request)['count'],2)
        revised=list((self.root/'.subtitle-edits').glob('*/revised.zh-CN.srt'))
        self.assertEqual(parse_srt(revised[0])[0].text,'人工修订测试')


class NotificationTests(unittest.TestCase):
    def test_notification_uses_no_private_data_and_failure_is_non_blocking(self):
        from translation_flow.notifications import notify_terminal
        with patch.dict(os.environ,{'SUBTITLE_API_KEY':'fake-test-secret'}),patch('translation_flow.notifications.subprocess.Popen') as popen:
            if os.name=='nt':
                self.assertTrue(notify_terminal('completed'))
                self.assertNotIn('SUBTITLE_API_KEY',popen.call_args.kwargs['env'])
                self.assertIn('Hidden',popen.call_args.args[0])
                popen.side_effect=OSError('unavailable')
                self.assertFalse(notify_terminal('failed'))
            self.assertFalse(notify_terminal('running'))

    def test_queue_notifies_once_after_validated_completion(self):
        from translation_flow.task_queue import TaskQueue
        from tests.test_task_queue import make_request, complete, Process
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); request=make_request(root)
            process=Process(); notices=[]
            queue=TaskQueue(root/'queue',launcher=lambda row,key:process,notifier=notices.append)
            queue.command(dict(action='enqueue',authorized=True,items=[dict(request_path=str(request))]))
            queue.tick(); complete(request); process.code=0; queue.tick(); queue.tick()
            self.assertEqual(notices,['completed'])


@unittest.skipUnless(os.environ.get('PRODUCT_MEDIA_TOOL_ROOT'),'generated-media tool root not configured')
class LibraryMediaTests(unittest.TestCase):
    def test_real_ffmpeg_edit_and_remux_preserves_two_audio_streams(self):
        tools=Path(os.environ['PRODUCT_MEDIA_TOOL_ROOT'])
        ffmpeg=tools/'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
        ffprobe=ffmpeg.with_name('ffprobe.exe')
        with tempfile.TemporaryDirectory(prefix='library-media-') as tmp:
            root=Path(tmp); request=fixture(root,'.translation-flow')
            media=root/'example.final.mkv'; media.unlink()
            subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10:duration=3',
                            '-f','lavfi','-i','sine=frequency=440:duration=3','-f','lavfi','-i','sine=frequency=880:duration=3',
                            '-i',str(root/'example.zh.srt'),'-map','0:v','-map','1:a','-map','2:a','-map','3:0',
                            '-c:v','libx264','-preset','ultrafast','-c:a','aac','-c:s','srt',
                            '-metadata:s:s:0','language=zho',str(media)],check=True,capture_output=True)
            original=media.read_bytes()
            review=library.load_review(request); review['rows'][0]['chinese']='会议明天开始。'
            saved=library.save_review(review)
            done=library.export_review(saved['revision_path'],str(tools))
            output=Path(done['media']); self.assertTrue(output.is_file())
            self.assertEqual(media.read_bytes(),original)
            facts=json.loads(subprocess.check_output([str(ffprobe),'-v','error','-show_streams','-of','json',str(output)]))
            self.assertEqual(len([s for s in facts['streams'] if s['codec_type']=='audio']),2)
            subtitle=subprocess.check_output([str(ffmpeg),'-v','error','-i',str(output),'-map','0:s:0','-f','srt','-']).decode('utf-8')
            self.assertIn('会议明天开始。',subtitle)
            report=library.read_json(Path(done['technical_report']))
            self.assertEqual(report['status'],'passed')
            self.assertEqual(done['human_review_status'],'pending')


if __name__=='__main__': unittest.main()
