"""CPU-only generated media; no real ASR, service, GPU, or picture model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext

from translation_flow.task_queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('PRODUCT_MEDIA_TOOL_ROOT'), 'set PRODUCT_MEDIA_TOOL_ROOT for generated-media acceptance')
class ProductMediaTests(unittest.TestCase):
    def test_formal_queue_three_combinations_resume_and_preserve_tracks(self):
        tools = Path(os.environ['PRODUCT_MEDIA_TOOL_ROOT'])
        ffmpeg = tools / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
        ffprobe = ffmpeg.with_name('ffprobe.exe')
        retained = os.environ.get('PRODUCT_MEDIA_OUTPUT_DIR')
        if retained:
            Path(retained).mkdir(parents=True, exist_ok=True)
        with (nullcontext(retained) if retained else tempfile.TemporaryDirectory(prefix='product-')) as tmp:
            root = Path(tmp)
            source = root / 'generated.mkv'
            srt = root / 'generated.srt'
            srt.write_text('1\n00:00:00,100 --> 00:00:01,500\nMeeting tomorrow.\n\n2\n00:00:02,000 --> 00:00:03,500\nBring the documents.\n', encoding='utf-8')
            subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10:duration=4',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=4',
                            '-i', str(srt), '-map','0:v','-map','1:a','-map','2:a','-map','3:0','-map','3:0',
                            '-c:v','libx264','-preset','ultrafast','-c:a','aac','-c:s','srt',
                            '-metadata:s:s:0','language=eng','-metadata:s:s:1','language=jpn',str(source)], check=True)
            before = (source.stat().st_size, source.stat().st_mtime_ns)
            first_launch = set()
            handles = []
            def launch(row, key):
                args = [sys.executable, str(ROOT/'tests/fixtures/product_worker.py'), '--request', row['request_path'], '--tool-root', str(tools)]
                if row['id'] not in first_launch:
                    if row['mode'] in ('picture_only', 'translation'):
                        args.append('--stop-after-work')
                    elif row['mode'] == 'translation_picture_final':
                        args.append('--stop-after-work' if row['job_id'].endswith('mp4') else '--fail-translation')
                    first_launch.add(row['id'])
                log = open(Path(row['request_path']).parent.parent.parent/'fixture-worker.log', 'ab')
                handles.append(log)
                return subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            queue = TaskQueue(root/'queue', launcher=launch)
            modes = [('translation','.translation-flow'),('picture_only','.picture-workflow'),('translation_picture_final','.full-workflow')]
            paths = []
            for target in ('mkv','mp4'):
                for mode, folder in modes:
                    name = f'{dict(translation="sub", picture_only="pic", translation_picture_final="both")[mode]}-{target}'
                    out = root / name
                    raw = dict(schema_version=1, job_id=name, input_video=str(source), output_dir=str(out))
                    if mode != 'translation': raw['workflow_mode']=mode
                    if mode != 'translation': raw['gates']=dict(picture_and_mux_30m_human_accepted=True)
                    if mode == 'picture_only': raw['output_target']=target
                    else: raw.update(profile_id='normal', provider=dict(id='codex',host='chatgpt.com',model='cli-default'),
                                     consent=dict(current_run=True,subtitle_text_only=True,audio_video_local=True), delivery=dict(additional_mp4=target=='mp4'))
                    path = out / folder / name / 'request.json'
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps(raw),encoding='utf-8')
                    paths.append(path)
            queue.command({'action':'enqueue','authorized':True,'items':[{'request_path':str(p)} for p in paths]})
            def drain():
                until=time.monotonic()+70
                while time.monotonic()<until:
                    queue.tick()
                    rows=queue.snapshot()['items']
                    if all(r['status'] in ('stopped','failed','completed') for r in rows): return rows
                    time.sleep(.05)
                self.fail('queue timeout')
            try:
                interrupted=drain()
                self.assertEqual(['stopped','stopped','failed','stopped','stopped','stopped'],[r['status'] for r in interrupted])
                queue.command({'action':'resume','authorized':True,'ids':[r['id'] for r in interrupted]})
                completed=drain()
                self.assertTrue(all(r['status']=='completed' for r in completed), [(r['status'],r.get('detail')) for r in completed])
                for row in completed:
                    target='mp4' if row['job_id'].endswith('mp4') else 'mkv'
                    outputs=row['outputs']
                    media=outputs.get('final_media') or outputs.get('final_mp4' if target=='mp4' else 'final_mkv')
                    self.assertTrue(media)
                    probe=json.loads(subprocess.check_output([str(ffprobe),'-v','error','-show_streams','-of','json',media]))
                    audio=[s for s in probe['streams'] if s['codec_type']=='audio']
                    subs=[s for s in probe['streams'] if s['codec_type']=='subtitle']
                    self.assertEqual(['aac','aac'],[s['codec_name'] for s in audio])
                    self.assertEqual(2 if row['mode']=='picture_only' else 1,len(subs))
                    self.assertEqual(['eng','jpn'] if row['mode']=='picture_only' else ['zho'],[s.get('tags',{}).get('language') for s in subs])
                    self.assertTrue(all(s['codec_name']==('mov_text' if target=='mp4' else 'subrip') for s in subs))
                    if row['mode']=='picture_only':
                        self.assertEqual(srt.read_bytes(), Path(outputs['source_subtitle_1']).read_bytes())
                        for index in range(2):
                            text=subprocess.check_output([str(ffmpeg),'-v','error','-i',media,'-map',f'0:s:{index}','-f','srt','-']).decode('utf-8')
                            self.assertIn('Meeting tomorrow.',text)
                            self.assertIn('Bring the documents.',text)
                    calls=[json.loads(line)['simulated'] for line in (Path(row['request_path']).parent.parent.parent/'fixture-model-calls.jsonl').read_text().splitlines()]
                    if row['mode']=='picture_only': self.assertEqual(['picture'],calls)
                    elif row['mode']=='translation': self.assertNotIn('picture',calls)
                    else: self.assertEqual(1,calls.count('picture'))
                    if row['mode']!='picture_only': self.assertEqual(1,calls.count('asr'))
                self.assertEqual(before,(source.stat().st_size,source.stat().st_mtime_ns))
                if retained:
                    (root/'acceptance-summary.json').write_text(json.dumps(dict(
                        scope='Generated media, simulated ASR/translation/picture, real formal queue/CLI/FFmpeg mux and decode',
                        live_services=False, real_gpu=False, human_review='pending', source_read_only=True,
                        interrupted=[dict(mode=r['mode'],job_id=r['job_id'],status=r['status']) for r in interrupted],
                        completed=[dict(mode=r['mode'],job_id=r['job_id'],status=r['status'],outputs=r['outputs']) for r in completed]
                    ),ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                for handle in handles: handle.close()
