"""Local background queue and JSON-stdin client; no third-party dependencies."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .task_queue import ACTIVE, FileLease, ROOT, TaskQueue, request_info, write_json

MAX_MESSAGE = 1024 * 1024


def serve(state_dir: Path, *, launcher=None):
    state_dir.mkdir(parents=True, exist_ok=True)
    with FileLease(state_dir / "worker.lock"):
        queue = TaskQueue(state_dir, launcher=launcher)
        token = secrets.token_urlsafe(32)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log request bodies, headers or credentials.

            def do_POST(self):
                if self.path != "/rpc" or not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + token
                ):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_MESSAGE:
                        raise ValueError("队列请求大小无效")
                    self.connection.settimeout(5)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("队列请求必须是 JSON 对象")
                    if payload.get("action") == "shutdown":
                        with queue.lock:
                            if any(r["status"] in ACTIVE for r in queue.state["items"]):
                                raise ValueError("还有任务运行；请先停止并等待安全结束")
                            queue.command({"action": "pause"})
                            queue.closed = True
                        result = queue.snapshot()
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        result = queue.command(payload)
                    response = {"ok": True, "queue": result}
                except Exception as exc:
                    # Do not reflect raw malformed inputs or provider credentials.
                    message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "队列请求无法处理；请核对任务文件和输入"
                    response = {"ok": False, "error": message}
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Closing a UI client never stops accepted work.

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        endpoint = state_dir / "worker.json"
        write_json(endpoint, {"port": server.server_address[1], "token": token, "pid": os.getpid()})
        thread = threading.Thread(target=queue.run, name="subtitle-queue", daemon=True)
        thread.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            queue.closed = True
            queue.wake.set()
            thread.join(timeout=2)
            server.server_close()
            endpoint.unlink(missing_ok=True)


def rpc(state_dir: Path, payload: dict, *, timeout=5):
    endpoint = json.loads((state_dir / "worker.json").read_text(encoding="utf-8"))
    port = endpoint["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("本机队列连接信息无效")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE:
        raise ValueError("队列请求过大，请分批加入")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/rpc", data=body,
        headers={"Authorization": "Bearer " + endpoint["token"], "Content-Type": "application/json"},
    )
    # Do not send local credentials through a user's HTTP proxy or redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        result = json.loads(response.read())
    return result


def ensure_worker(state_dir: Path):
    try:
        return rpc(state_dir, {"action": "status"}, timeout=0.5)
    except (OSError, ValueError, KeyError):
        pass
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("SUBTITLE_API_KEY", None)
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, "-m", "translation_flow.queue_cli", "--serve", "--state-dir", str(state_dir)],
                     cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **options)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            return rpc(state_dir, {"action": "status"}, timeout=0.5)
        except (OSError, ValueError, KeyError):
            time.sleep(0.1)
    raise RuntimeError("无法连接本机后台队列；请检查队列目录是否可写")


def run_request(path: Path, digest: str, tool_root: str):
    """Own a per-request lease, then call an unchanged formal entry point."""
    path = path.resolve()
    try:
        with FileLease(path.parent / "queue.run.lock"):
            info = request_info(path)
            if not digest or info["request_digest"] != digest:
                raise ValueError("已排队的正式请求被改变，拒绝启动")
            if tool_root:
                os.environ["TRANSLATION_FLOW_TOOL_ROOT"] = tool_root
            if info["provider"] in {"", "codex"}:
                os.environ.pop("SUBTITLE_API_KEY", None)
            module = importlib.import_module(info["module"])
            return module.main(["--request", str(path)])
    except Exception:
        # Parent reports failed startup without echoing private request contents.
        return 1


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Windows PowerShell/.NET Framework may prepend a UTF-8 BOM to
            # redirected stdin after the formal launcher selects chcp 65001.
            # Accept both forms; stdout/stderr remain ordinary UTF-8 JSON.
            stream.reconfigure(encoding="utf-8-sig" if stream is sys.stdin else "utf-8")
    parser = argparse.ArgumentParser(description="本机字幕任务后台队列")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--run-request", type=Path)
    parser.add_argument("--request-digest", default="")
    parser.add_argument("--tool-root", default="")
    args = parser.parse_args(argv)
    if args.run_request:
        return run_request(args.run_request, args.request_digest, args.tool_root)
    try:
        if args.state_dir is None:
            raise ValueError("请指定本机队列目录 --state-dir")
        state_dir = args.state_dir.resolve()
        if args.serve:
            serve(state_dir)
            return 0
        raw = sys.stdin.read(MAX_MESSAGE + 1)
        if len(raw.encode("utf-8")) > MAX_MESSAGE:
            raise ValueError("队列请求过大，请分批加入")
        try:
            payload = json.loads(raw) if raw.strip() else {"action": "status"}
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "本机队列请求不是有效的 JSON；请重新打开软件后重试", "error_code": "INVALID_QUEUE_JSON"}, ensure_ascii=False))
            return 1
        ensure_worker(state_dir)
        result = rpc(state_dir, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception:
        print(json.dumps({"ok": False, "error": "无法连接或处理本机队列请求；请检查任务文件、目录和后台状态"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
