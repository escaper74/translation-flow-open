"""Single-file, loopback-only playback. No uploads, media writes or model APIs."""
from __future__ import annotations
import argparse
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import webbrowser

from .setup import DEFAULT, FILES

WEB = Path(__file__).parent / "web"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def tools_from_root(root: Path):
    from translation_flow.asr import ToolPaths
    import sys
    tools = ToolPaths.from_root(root, root / "asr-lab", python=Path(sys.executable))
    return tools.ffmpeg, tools.ffprobe

def byte_range(header: str | None, size: int):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise ValueError("Invalid range")
    left, right = match.groups()
    start = int(left) if left else max(0, size - int(right))
    end = min(int(right), size - 1) if left and right else size - 1
    if size <= 0 or start >= size or start > end:
        raise ValueError("Unsatisfiable range")
    return start, end, True

class Playback:
    def __init__(self, source: Path, tool_root: Path, runtime: Path = DEFAULT):
        self.source = source.resolve(strict=True)
        if not self.source.is_file():
            raise ValueError("请选择本地视频文件")
        self.ffmpeg, self.ffprobe = tools_from_root(tool_root)
        self.runtime = runtime.resolve()
        missing = [name for name in [*FILES, "face_landmarker.task"] if not (runtime / name).is_file()]
        if missing:
            raise ValueError("美颜运行组件未准备：请先运行 python -m beauty_player.setup（仅首次下载）")
        probe = subprocess.run([str(self.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(self.source)],
                               capture_output=True, timeout=30, creationflags=HIDDEN)
        if probe.returncode:
            raise ValueError("视频无法读取，请检查源文件是否完整")
        self.probe = json.loads(probe.stdout)
        streams = self.probe.get("streams", [])
        video = next((s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
        if not video:
            raise ValueError("没有可播放的视频轨")
        self.duration = float(self.probe["format"].get("duration", 0))
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise ValueError("无法取得视频时长")
        self.video_index = video["index"]
        self.audio = next((s for s in streams if s["codec_type"] == "audio"), None)
        subtitles = [s for s in streams if s["codec_type"] == "subtitle"]
        self.subtitle = next((s for s in subtitles if s.get("tags", {}).get("language") in ("zho", "chi", "zh")),
                             next((s for s in subtitles if s.get("disposition", {}).get("default")), None))
        fmt = self.probe["format"].get("format_name", "")
        self.direct = "mp4" in fmt or ("webm" in fmt and video["codec_name"] in ("vp8", "vp9", "av1"))
        if video["codec_name"] not in ("h264", "hevc", "vp8", "vp9", "av1"):
            raise ValueError("此视频编码暂不适合实时播放器；请用原播放器观看。本工具不会自动转码。")
        if not self.direct and self.audio and self.audio["codec_name"] not in ("aac", "mp3", "opus"):
            raise ValueError("此音轨无法直接供浏览器播放；不会自动转码或静音，请用原播放器观看。")
        self.metadata = dict(title=self.source.name, duration=self.duration, direct=self.direct,
                             width=video["width"], height=video["height"], codec=video["codec_name"],
                             subtitles=bool(self.subtitle and self.subtitle["codec_name"] in ("subrip", "ass", "ssa", "webvtt", "mov_text")),
                             audio_tracks=sum(s["codec_type"] == "audio" for s in streams))
        self.token = secrets.token_urlsafe(24)
        self.last_seen = time.monotonic()
        self.children: set[subprocess.Popen] = set()
        self.lock = threading.Lock()
        self.subtitle_cache = None

    def stream_command(self, start, compatibility=False):
        args = [str(self.ffmpeg), "-nostdin", "-v", "error", "-readrate", "1.5", "-ss", str(start), "-i", str(self.source),
                "-map", f"0:{self.video_index}"]
        if self.audio:
            args += ["-map", f"0:{self.audio['index']}"]
        if compatibility:
            # Preview only: bounded CPU H.264 stream for browsers without HEVC.
            # No output file, no original-media changes, no beauty export.
            args += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "22", "-threads", "4",
                     "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear,setsar=1",
                     "-c:a", "copy", "-tag:v", "avc1"]
        else:
            args += ["-c", "copy"]
            if self.metadata["codec"] in ("hevc", "h264"):
                args += ["-tag:v", "hvc1" if self.metadata["codec"] == "hevc" else "avc1"]
        return args + [
                       "-avoid_negative_ts", "make_zero", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                       "-frag_duration", "1000000", "-f", "mp4", "pipe:1"]

    def seek_plan(self, requested, compatibility=False):
        if not math.isfinite(requested) or not 0 <= requested < self.duration:
            raise ValueError("Invalid seek")
        if self.direct or compatibility or requested == 0:
            return {"start": requested}
        # Input-side stream copy starts at a keyframe. Keep playback time and
        # subtitles on that real anchor, not the requested (possibly later) time.
        probe = subprocess.run([str(self.ffprobe), "-v", "error", "-select_streams", str(self.video_index),
                                "-read_intervals", f"{requested}%+#1", "-show_packets", "-show_entries", "packet=pts_time,flags",
                                "-of", "json", str(self.source)], capture_output=True, timeout=30, creationflags=HIDDEN)
        if probe.returncode:
            raise ValueError("Cannot locate playback keyframe")
        packets = json.loads(probe.stdout).get("packets", [])
        if not packets or "K" not in packets[0].get("flags", ""):
            raise ValueError("Cannot locate playback keyframe")
        start = float(packets[0]["pts_time"]) - float(self.probe["format"].get("start_time", 0))
        if not math.isfinite(start):
            raise ValueError("Invalid keyframe timestamp")
        return {"start": max(0, min(start, self.duration - .001))}

    def close(self):
        with self.lock:
            children = list(self.children)
        for child in children:
            if child.poll() is None:
                child.terminate()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # File names and capability URLs never go into access logs.

    @property
    def playback(self):
        return self.server.playback

    def route(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host or self.headers.get("Origin", f"http://{host}") != f"http://{host}":
            return None
        parts = urlsplit(self.path)
        prefix = "/" + self.playback.token + "/"
        if not parts.path.startswith(prefix):
            return None
        self.playback.last_seen = time.monotonic()
        return parts.path[len(prefix):], parse_qs(parts.query)

    def send_headers(self, status, mime, length=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; worker-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))

    def send_bytes(self, data, mime="application/json", status=200):
        self.send_headers(status, mime, len(data)); self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = self.route()
        if route is None:
            self.send_error(404); return
        name, query = route
        p = self.playback
        try:
            if name == "metadata":
                self.send_bytes(json.dumps(p.metadata, ensure_ascii=False).encode()); return
            if name == "ping":
                self.send_bytes(b'{}'); return
            if name == "source":
                self.send_source(); return
            if name == "stream":
                self.send_stream(float(query.get("start", ["0"])[0]), query.get("compat") == ["1"]); return
            if name == "seek":
                self.send_bytes(json.dumps(p.seek_plan(float(query.get("start", ["0"])[0]), query.get("compat") == ["1"])).encode()); return
            if name == "subtitles.vtt":
                if p.subtitle_cache is None:
                    if not p.metadata["subtitles"]:
                        p.subtitle_cache = b"WEBVTT\n\n"
                    else:
                        result = subprocess.run([str(p.ffmpeg), "-nostdin", "-v", "error", "-i", str(p.source),
                                                 "-map", f"0:{p.subtitle['index']}", "-f", "webvtt", "pipe:1"],
                                                capture_output=True, timeout=60, creationflags=HIDDEN)
                        if result.returncode:
                            self.send_error(500, "Subtitle extraction failed"); return
                        p.subtitle_cache = result.stdout
                self.send_bytes(p.subtitle_cache, "text/vtt; charset=utf-8"); return
            assets = {"": WEB / "index.html", "index.html": WEB / "index.html", "player.css": WEB / "player.css",
                      "player.js": WEB / "player.js", "renderer.js": WEB / "renderer.js", "tracking.js": WEB / "tracking.js",
                      "tracking-worker.js": WEB / "tracking-worker.js", "toolbar.html": WEB / "toolbar.html",
                      "toolbar.js": WEB / "toolbar.js"}
            assets.update({"runtime/" + f: p.runtime / f for f in [*FILES, "face_landmarker.task"]})
            path = assets.get(name)
            if path is None:
                self.send_error(404); return
            mime = "text/javascript" if path.suffix in (".js", ".mjs") else "application/wasm" if path.suffix == ".wasm" else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_bytes(path.read_bytes(), mime)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (ValueError, OSError):
            self.send_error(400, "Playback request failed")

    def send_source(self):
        p = self.playback
        size = p.source.stat().st_size
        try:
            start, end, partial = byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
        self.send_headers(206 if partial else 200, "video/mp4" if "mp4" in p.probe["format"]["format_name"] else "video/webm", end - start + 1)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD": return
        with p.source.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                data = stream.read(min(256 * 1024, remaining))
                if not data: break
                self.wfile.write(data); remaining -= len(data)

    def send_stream(self, start, compatibility=False):
        p = self.playback
        if not math.isfinite(start) or not 0 <= start < p.duration:
            raise ValueError("Invalid seek")
        self.send_headers(200, "video/mp4"); self.send_header("Connection", "close"); self.end_headers()
        if self.command == "HEAD": return
        self.close_connection = True
        child = subprocess.Popen(p.stream_command(start, compatibility), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=HIDDEN)
        with p.lock: p.children.add(child)
        try:
            while chunk := child.stdout.read1(64 * 1024):
                self.wfile.write(chunk); self.wfile.flush()
        finally:
            if child.poll() is None: child.terminate()
            child.wait(timeout=10); child.stdout.close()
            with p.lock: p.children.discard(child)

def create_server(playback):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.playback = playback
    return server

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--tool-root", type=Path, default=Path(__file__).resolve().parents[2] / "workflow/tools")
    parser.add_argument("--runtime", type=Path, default=DEFAULT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    playback = Playback(args.media, args.tool_root, args.runtime)
    server = create_server(playback)
    url = f"http://127.0.0.1:{server.server_port}/{playback.token}/"
    if not args.no_browser:
        edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        if edge.is_file():
            subprocess.Popen([str(edge), "--new-window", "--app=" + url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    print(json.dumps({"url": url}), flush=True)
    def expire():
        while time.monotonic() - playback.last_seen < 180:
            time.sleep(10)
        playback.close(); server.shutdown()
    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        playback.close(); server.server_close()

if __name__ == "__main__":
    main()
