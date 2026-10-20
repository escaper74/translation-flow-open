"""Opt-in real Edge/WebGL test on a neutral generated 30-second portrait clip.

python -m tests.beauty_browser_smoke --media <portrait.mp4|mkv> --output <new-dir>
No user browser profile, external service or production media is used.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from beauty_player.server import Handler, Playback, WEB, create_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--media', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tool-root', type=Path, default=Path('workflow/tools'))
    parser.add_argument('--node', default='node')
    parser.add_argument('--app-window', action='store_true', help='Match the formal Edge --app launch')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    playback = Playback(args.media, args.tool_root.resolve(), args.tool_root.resolve() / 'beauty-player')
    received = threading.Event()
    results = []

    class TestHandler(Handler):
        def do_GET(self):
            route = self.route()
            if route and route[0] == '':
                body = (WEB / 'index.html').read_text(encoding='utf-8').replace('</body>', '<script type="module" src="test-driver.js"></script></body>')
                self.send_bytes(body.encode(), 'text/html'); return
            if route and route[0] == 'test-driver.js':
                self.send_bytes((Path(__file__).parent / 'fixtures/beauty_browser.js').read_bytes(), 'text/javascript'); return
            super().do_GET()

        def do_POST(self):
            route = self.route()
            if not route or route[0] != 'test-result':
                self.send_error(404); return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length < 10_000_000:
                self.send_error(400); return
            results.append(json.loads(self.rfile.read(length)))
            self.send_bytes(b'{}'); received.set()

    server = create_server(playback)
    server.RequestHandlerClass = TestHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    edge = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft/Edge/Application/msedge.exe'
    url = f'http://127.0.0.1:{server.server_port}/{playback.token}/'
    source_before = (args.media.stat().st_size, args.media.stat().st_mtime_ns)
    with (args.output / 'browser.log').open('wb') as log:
        child = subprocess.Popen([str(edge), '--headless=new', '--no-first-run', '--no-default-browser-check',
                                  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,1000', '--remote-debugging-port=0',
                                  '--user-data-dir=' + str(args.output.resolve() / 'profile'), '--app=' + url if args.app_window else url],
                                 stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            devtools = args.output.resolve() / 'profile/DevToolsActivePort'
            deadline = time.monotonic() + 20
            while not devtools.exists():
                # Edge may hand off to another process and exit 0 before the
                # isolated profile's debugging endpoint is ready.
                if child.poll() not in (None, 0) or time.monotonic() > deadline:
                    raise TimeoutError('Isolated Edge did not start; see browser.log')
                time.sleep(.1)
            port = devtools.read_text().splitlines()[0]
            windows = subprocess.run([args.node, str(Path(__file__).parent / 'fixtures/beauty_windows.mjs'),
                                      port, str(args.output.resolve()), url], capture_output=True, text=True, encoding='utf-8', timeout=110)
            (args.output / 'window-driver.log').write_text(windows.stdout + windows.stderr, encoding='utf-8')
            if not received.wait(1):
                raise TimeoutError('Browser did not report; see browser.log')
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
            playback.close(); server.shutdown(); server.server_close(); thread.join(3)
    result = results[0]
    result['windows'] = json.loads((args.output / 'window-result.json').read_text(encoding='utf-8'))
    if result.get('image'):
        (args.output / 'beauty-frame.png').write_bytes(base64.b64decode(result.pop('image').split(',', 1)[1]))
    result['source_unchanged'] = source_before == (args.media.stat().st_size, args.media.stat().st_mtime_ns)
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] != 'passed' or result['windows']['status'] != 'passed' or not result['source_unchanged']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
