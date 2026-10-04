import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

from beauty_player.server import Playback, byte_range, create_server
from beauty_player.setup import FILES


class BeautyRangeTests(unittest.TestCase):
    def test_full_and_partial_ranges(self):
        self.assertEqual(byte_range(None, 10), (0, 9, False))
        self.assertEqual(byte_range('bytes=2-4', 10), (2, 4, True))
        self.assertEqual(byte_range('bytes=8-', 10), (8, 9, True))
        self.assertEqual(byte_range('bytes=-3', 10), (7, 9, True))
        self.assertEqual(byte_range('bytes=2-99', 10), (2, 9, True))

    def test_invalid_ranges(self):
        for value in ('bytes=10-', 'bytes=5-1', 'bytes=-0', 'bytes=-', 'bytes=0-2,4-6', 'garbage'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                byte_range(value, 10)


class BeautyPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mkv'
        self.source.write_bytes(b'never modify original')
        for name in [*FILES, 'face_landmarker.task']:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'test asset')
        self.probe = {'format': {'duration': '30', 'format_name': 'matroska,webm'}, 'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'hevc', 'width': 1280, 'height': 720},
            {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'},
            {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'zho'}},
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def playback(self):
        with patch('beauty_player.server.tools_from_root', return_value=(Path('ffmpeg'), Path('ffprobe'))), \
             patch('beauty_player.server.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(self.probe).encode())):
            return Playback(self.source, self.root, self.root)

    def test_mkv_is_stream_copy_to_pipe_without_media_output(self):
        playback = self.playback()
        self.assertFalse(playback.direct)
        self.assertTrue(playback.metadata['subtitles'])
        command = playback.stream_command(12.5)
        self.assertEqual(command[command.index('-c') + 1], 'copy')
        self.assertEqual(command[-1], 'pipe:1')
        self.assertEqual(command[command.index('-ss') + 1], '12.5')
        self.assertIn('hvc1', command)
        self.assertEqual(self.source.read_bytes(), b'never modify original')

    def test_mp4_direct_uses_probed_container_not_extension(self):
        self.probe['format']['format_name'] = 'mov,mp4,m4a,3gp,3g2,mj2'
        self.assertTrue(self.playback().direct)

    def test_mkv_seek_uses_actual_keyframe_for_timeline(self):
        playback = self.playback()
        reply = {'packets': [{'pts_time': '8.354', 'flags': 'K__'}]}
        with patch('beauty_player.server.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(reply).encode())):
            self.assertEqual(playback.seek_plan(12), {'start': 8.354})
        for value in (-1, 30, float('nan')):
            with self.assertRaises(ValueError):
                playback.seek_plan(value)

    def test_hevc_preview_fallback_is_cpu_pipe_not_source_rewrite(self):
        playback = self.playback()
        command = playback.stream_command(12, True)
        self.assertIn('libx264', command)
        self.assertEqual(command[command.index('-c:a') + 1], 'copy')
        self.assertEqual(command[-1], 'pipe:1')
        self.assertNotIn('nvenc', ' '.join(command))
        self.assertEqual(playback.seek_plan(12, True), {'start': 12})

    def test_cover_art_is_not_main_video(self):
        self.probe['streams'].insert(0, {'index': 8, 'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}})
        self.assertEqual(self.playback().video_index, 0)

    def test_unplayable_codec_has_explicit_message_not_silent_transcode(self):
        self.probe['streams'][0]['codec_name'] = 'ffv1'
        with self.assertRaisesRegex(ValueError, '不会自动转码'):
            self.playback()

    def test_missing_runtime_does_not_auto_download(self):
        (self.root / 'face_landmarker.task').unlink()
        with self.assertRaisesRegex(ValueError, '仅首次下载'):
            self.playback()

    def test_broken_duration_rejected(self):
        for duration in ('NaN', '-1', '0', 'Infinity'):
            self.probe['format']['duration'] = duration
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.playback()


class BeautyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / 'source.mp4'
        self.source.write_bytes(b'0123456789')
        playback = SimpleNamespace(token='test-token', source=self.source, runtime=Path(self.temp.name),
                                   metadata={'title': 'source.mp4'}, last_seen=0,
                                   probe={'format': {'format_name': 'mp4'}}, subtitle_cache=b'WEBVTT\n\n')
        self.server = create_server(playback)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.base = self.origin + '/test-token/'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, route='', headers=None, method='GET'):
        return urllib.request.urlopen(urllib.request.Request(self.base + route, headers=headers or {}, method=method), timeout=3)

    def test_local_shell_and_csp(self):
        with self.request() as response:
            self.assertIn('实时美颜'.encode(), response.read())
            self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')

    def test_toolbar_is_a_separate_local_document(self):
        with self.request() as response:
            player = response.read().decode()
            self.assertIn('id="beauty-tools"', player)
            self.assertNotIn('id="smooth"', player)
            self.assertNotIn('<aside', player)
        with self.request('toolbar.html') as response:
            toolbar = response.read().decode()
            self.assertIn('id="smooth"', toolbar)
            self.assertNotIn('<video', toolbar)
        with self.request('toolbar.js') as response:
            self.assertIn('event.source!==owner', response.read().decode())

    def test_seek_range_reads_only_requested_bytes(self):
        with self.request('source', {'Range': 'bytes=3-6'}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b'3456')
            self.assertEqual(response.headers['Content-Range'], 'bytes 3-6/10')
        with self.request('source', method='HEAD') as response:
            self.assertEqual(response.read(), b'')
            self.assertEqual(response.headers['Content-Length'], '10')
        self.assertEqual(self.source.read_bytes(), b'0123456789')

    def test_unrelated_paths_and_external_origin_are_not_exposed(self):
        for route, headers in [('other.mp4', {}), ('../../AGENTS.md', {}),
                               ('metadata', {'Origin': 'https://example.com'}),
                               ('metadata', {'Host': 'example.com'})]:
            with self.subTest(route=route, headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route, headers)
            self.assertEqual(error.exception.code, 404)

    def test_wrong_token_and_no_upload_route(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.origin + '/wrong-token/source', timeout=3)
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('source', method='POST')
        self.assertEqual(error.exception.code, 501)

    def test_invalid_range_returns_416(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('source', {'Range': 'bytes=999-'})
        self.assertEqual(error.exception.code, 416)


@unittest.skipUnless(os.name == 'nt', 'WPF bridge requires Windows')
class BeautyDesktopTests(unittest.TestCase):
    def test_real_bridge_quotes_path_without_key_and_allows_second_open(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='beauty-bridge-') as tmp:
            work = Path(tmp)
            package = work / 'beauty_player'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'server.py').write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "with Path('launches.jsonl').open('a',encoding='utf-8') as f:\n"
                " f.write(json.dumps({'args':sys.argv[1:],'has_key':bool(os.environ.get('SUBTITLE_API_KEY'))})+'\\n')\n"
                "print(json.dumps({'url':'http://127.0.0.1:1/test-fixture/'}),flush=True)\n", encoding='utf-8')
            source = work / '字幕 中文 sample.mp4'
            source.write_bytes(b'test-only path; never decoded')
            script = f"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName PresentationFramework
. '{root}/desktop/TranslationFlowProcessBridge.ps1'
. '{root}/desktop/TranslationFlowBeauty.ps1'
$Window=$null
$ProjectRoot='{work}'
$PythonPathBox=New-Object Windows.Controls.TextBox
$PythonPathBox.Text='{sys.executable}'
$ToolRootBox=New-Object Windows.Controls.TextBox
$ToolRootBox.Text='{work}/tools with spaces'
$env:SUBTITLE_API_KEY='local-fake-key-must-not-propagate'
foreach($attempt in 1..2) {{
  Show-BeautyPlayer '{source}'
  $end=[DateTime]::UtcNow.AddSeconds(10)
  while($script:BeautyStartup.Busy) {{
    if([DateTime]::UtcNow -gt $end){{throw 'Beauty startup stayed busy'}}
    $frame=New-Object Windows.Threading.DispatcherFrame
    $stop=New-Object Windows.Threading.DispatcherTimer
    $stop.Interval=[TimeSpan]::FromMilliseconds(100)
    $stop.Add_Tick({{$stop.Stop();$frame.Continue=$false}})
    $stop.Start();[Windows.Threading.Dispatcher]::PushFrame($frame)
  }}
}}
"""
            test_file = work / 'test.ps1'
            test_file.write_text(script, encoding='utf-8-sig')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass', '-File', str(test_file)],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in (work / 'launches.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertFalse(row['has_key'])
                self.assertEqual(row['args'], ['--media', str(source), '--tool-root', str(work) + '/tools with spaces'])


if __name__ == '__main__':
    unittest.main()
