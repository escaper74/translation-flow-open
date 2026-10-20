"""Persistent sequential queue for the existing subtitle and remux entry points.

The queue owns scheduling only. Requests, checkpoints and technical reports stay
owned by their existing backends. Credentials never enter queue state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MODULES = {
    "translation": ("translation_flow.cli", ".translation-flow", "job.result.json"),
    "srt_translation": ("translation_flow.srt_desktop", ".srt-translation", "job.result.json"),
    "media_repackage": ("media_planning.cli", ".media-repackage", "technical-validation.report.json"),
    "translation_picture_final": ("full_workflow.cli", ".full-workflow", "workflow.result.json"),
    "picture_only": ("full_workflow.picture_cli", ".picture-workflow", "job.result.json"),
}
ACTIVE = {"starting", "running", "stopping"}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class FileLease:
    """OS lease: prevent two reopened windows/workers from launching one job twice."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        try:
            self.handle.seek(0, 2)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.handle.close()
            raise

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def process_stamp(pid: int) -> str | None:
    """Creation identity, so an unrelated process reusing a PID is never adopted."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                raise OSError("无法核对后台任务进程身份")
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def request_info(path: str | Path) -> dict:
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("任务请求必须是 JSON 对象")
    mode = raw.get("workflow_mode", "translation")
    if mode not in MODULES:
        raise ValueError("不支持的任务类型")
    public = {"schema_version", "workflow_mode", "job_id", "output_dir"}
    public |= ({"source_media", "output_target"} if mode == "media_repackage" else
               {"input_video", "output_target", "gates"} if mode == "picture_only" else
               {"input_srt" if mode == "srt_translation" else "input_video", "profile_id", "provider", "consent", "delivery"})
    if mode == "translation_picture_final":
        public.add("gates")
    if mode in {"translation", "translation_picture_final"}:
        public.add("asr_a_model")
    if set(raw) - public:
        raise ValueError("任务请求含未知字段；密钥只能通过本次进程传递")
    if mode == "picture_only":
        from full_workflow.picture import PictureRequest
        request = PictureRequest.from_mapping(raw)
        source, provider = request.input_video, ""
    elif mode == "media_repackage":
        from media_planning.contracts import MediaRepackageRequest
        request = MediaRepackageRequest.from_mapping(raw)
        source = request.source_media
        provider = ""
    else:
        for key, allowed in {
            "provider": {"id", "host", "model", "endpoint_profile", "max_output_tokens"},
            "consent": {"current_run", "subtitle_text_only", "audio_video_local", "provider"},
            "delivery": {"additional_mp4"},
        }.items():
            value = raw.get(key, {})
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError("任务配置含未知字段；请使用正式请求")
        if mode == "srt_translation":
            from .srt_desktop import load_request
            request = load_request(path)
        elif mode == "translation_picture_final":
            from full_workflow.contracts import FullWorkflowRequest
            gate = raw.get("gates", {})
            if not isinstance(gate, dict) or set(gate) - {"picture_and_mux_30m_human_accepted"}:
                raise ValueError("无效的路线验收配置")
            request = FullWorkflowRequest.from_mapping(raw).translation
        else:
            from .contracts import JobRequest
            request = JobRequest.from_mapping(raw)
        source = request.input_video
        provider = request.provider.id
    module, directory, result = MODULES[mode]
    job_root = request.output_dir / directory / request.job_id
    if path != job_root / "request.json":
        raise ValueError("请使用输出目录下的正式 request.json")
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    queue_id = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]
    return dict(id=queue_id, job_id=request.job_id, mode=mode, module=module,
                request_path=str(path), request_digest=fingerprint, source=str(source),
                provider=provider, result_path=str(job_root / result))


class TaskQueue:
    def __init__(self, state_dir: Path, *, launcher=None, notifier=None):
        self.state_dir = Path(state_dir).resolve()
        self.path = self.state_dir / "queue.json"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = False
        self.credentials: dict[str, str] = {}
        self.processes: dict[str, Any] = {}
        self.launcher = launcher or self._launch
        from .notifications import notify_terminal
        self.notifier = notifier or (notify_terminal if launcher is None else lambda status: False)
        self.state = {"schema_version": 1, "paused": False, "items": []}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("schema_version") != 1 or not isinstance(self.state.get("items"), list):
                raise ValueError("队列记录无法识别，请保留现场")
            # A fresh worker has no previous in-memory credentials or consent.
            self.state["paused"] = True
            for row in self.state["items"]:
                if row["status"] in ACTIVE:
                    stamp = process_stamp(row.get("child_pid", 0))
                    if not stamp or stamp != row.get("child_stamp"):
                        self._read_events(row)
                        self._finish(row, None)
                elif row["status"] == "queued":
                    row.update(status="paused", message="后台已重启；确认原设置后继续")
        self._save()

    def _save(self):
        write_json(self.path, self.state)

    def snapshot(self):
        with self.lock:
            result = {**copy.deepcopy(self.state), "worker_pid": os.getpid(), "state_dir": str(self.state_dir),
                      "project_root": str(ROOT), "python_path": sys.executable}
            for row in result["items"]:
                row["has_session_key"] = bool(self.credentials.get(row["id"]))
            return result

    def _public_text(self, value):
        text = str(value)
        for key in self.credentials.values():
            if key:
                text = text.replace(key, "[已隐藏]")
        return text[:1600]

    def _row(self, queue_id):
        for row in self.state["items"]:
            if row["id"] == queue_id:
                return row
        raise ValueError("没有找到所选队列任务")

    def command(self, payload: dict):
        with self.lock:
            action = payload.get("action", "status")
            if action == "enqueue":
                items = payload.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("请至少选择一个任务")
                prepared = []
                skipped = []
                seen = set()
                for item in items:
                    try:
                        info = request_info(item["request_path"])
                    except (OSError, ValueError) as exc:
                        skipped.append({"request_path": str(item["request_path"]),
                                        "message": self._public_text(exc)})
                        continue
                    if info["id"] in seen:
                        continue
                    seen.add(info["id"])
                    if info["provider"] and payload.get("authorized") is not True:
                        raise ValueError("请确认本批任务的字幕外发范围与翻译服务")
                    key = item.get("api_key", "")
                    if not isinstance(key, str):
                        raise ValueError("API Key 必须是文本")
                    tool_root = item.get("tool_root", "")
                    if not isinstance(tool_root, str):
                        raise ValueError("工具目录必须是文本")
                    existing = next((r for r in self.state["items"] if r["id"] == info["id"]), None)
                    if existing and existing["request_digest"] != info["request_digest"]:
                        raise ValueError("原请求已改变；请另建正式任务，不要覆盖恢复设置")
                    prepared.append((info, key, tool_root, existing))
                self.state["submission"] = {
                    "added": [info["id"] for info, _, _, existing in prepared if not existing],
                    "existing": [info["id"] for info, _, _, existing in prepared if existing],
                    "skipped": skipped,
                    "message": f"已加入 {sum(not existing for _, _, _, existing in prepared)} 个任务；{len(skipped)} 个输入需要重新选择",
                }
                if not prepared:
                    self._save()
                    raise ValueError(skipped[0]["message"])
                for info, key, tool_root, existing in prepared:
                    if existing:
                        continue  # Repeated clicks never restart a task.
                    paused = self.state["paused"]
                    info.update(status="paused" if paused else "queued",
                                message="队列已暂停；确认后继续" if paused else "等待处理", tool_root=tool_root,
                                progress=0, stage="", event_offset=0, last_seq=0)
                    self.state["items"].append(info)
                    self.credentials[info["id"]] = key if info["provider"] not in {"", "codex"} else ""
            elif action == "resume":
                ids = payload.get("ids", [])
                if not isinstance(ids, list) or not ids:
                    raise ValueError("请选择需要继续的任务")
                selected = [self._row(qid) for qid in dict.fromkeys(ids)]
                for row in selected:
                    if row["status"] in ACTIVE or row["status"] == "completed":
                        raise ValueError("运行中或已完成的任务无需重新启动")
                    if row["provider"] and payload.get("authorized") is not True:
                        raise ValueError("请重新确认所选任务的字幕外发范围")
                    if request_info(row["request_path"])["request_digest"] != row["request_digest"]:
                        raise ValueError("原请求已改变，不能继续使用原任务身份")
                keys = payload.get("credentials", {})
                if not isinstance(keys, dict) or any(not isinstance(v, str) for v in keys.values()):
                    raise ValueError("本次密钥配置无效")
                if self.state["paused"]:
                    # Older workers could leave globally paused rows as queued.
                    # Resuming selected tasks must not release any other rows.
                    for row in self.state["items"]:
                        if row["status"] == "queued":
                            row.update(status="paused", message="队列已暂停；确认后继续")
                for row in selected:
                    if row["provider"] not in {"", "codex"}:
                        self.credentials[row["id"]] = keys.get(row["id"]) or self.credentials.get(row["id"], "")
                    row.update(status="queued", message="等待从检查点继续")
                self.state["paused"] = False
            elif action in {"stop", "pause"}:
                queue_id = payload.get("id")
                selected = [self._row(queue_id)] if queue_id else self.state["items"]
                if not queue_id:
                    self.state["paused"] = True
                for row in selected:
                    if row["status"] == "queued":
                        row.update(status="paused", message="已保留待执行任务")
                    elif action == "stop" and row["status"] in ACTIVE:
                        row.update(status="stopping", message="正在安全停止；已完成部分会保留")
                        self._signal_stop(row)
            elif action != "status":
                raise ValueError("不支持的队列操作")
            if action != "status":
                self._save()
                self.wake.set()
            return self.snapshot()

    def _signal_stop(self, row):
        (Path(row["request_path"]).parent / "cancel.requested").write_text("stop", encoding="utf-8")

    def _launch(self, row, key):
        env = dict(os.environ)
        env.pop("SUBTITLE_API_KEY", None)
        if key:
            env["SUBTITLE_API_KEY"] = key
        env["PYTHONIOENCODING"] = "utf-8"
        args = [sys.executable, "-m", "translation_flow.queue_cli", "--run-request", row["request_path"],
                "--request-digest", row["request_digest"]]
        if row["tool_root"]:
            args += ["--tool-root", row["tool_root"]]
        return subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _read_events(self, row):
        path = Path(row["request_path"]).parent / "events.jsonl"
        if not path.exists():
            return False
        changed = False
        with path.open("rb") as handle:
            handle.seek(row.get("event_offset", 0))
            for _ in range(1000):
                line = handle.readline(1024 * 1024)
                if not line or not line.endswith(b"\n"):
                    break
                row["event_offset"] = handle.tell()
                try:
                    item = json.loads(line)
                    if item.get("job_id") != row["job_id"] or not isinstance(item.get("seq"), int):
                        continue
                    if item["seq"] <= row.get("last_seq", 0):
                        continue
                    row["last_seq"] = item["seq"]
                    row["last_event"] = item.get("event", "")
                    if isinstance(item.get("stage"), str):
                        row["stage"] = item["stage"]
                    if isinstance(item.get("overall_percent"), (int, float)):
                        row["progress"] = min(100, max(0, item["overall_percent"]))
                    if item.get("event") in {"job_completed", "job_failed", "job_cancelled"}:
                        row["terminal_event"] = item["event"]
                        if item["event"] != "job_completed":
                            row["detail"] = self._public_text(item.get("message", ""))
                            row["error_code"] = self._public_text(item.get("error_code", ""))
                            row["recovery_hint"] = self._public_text(item.get("recovery_hint", ""))
                    elif item.get("event") == "warning":
                        row["warning_count"] = row.get("warning_count", 0) + 1
                        row["last_warning"] = self._public_text(item.get("message", ""))
                    changed = True
                except (ValueError, TypeError, AttributeError):
                    continue
        return changed

    def _success_outputs(self, row):
        report = json.loads(Path(row["result_path"]).read_text(encoding="utf-8"))
        if row["mode"] == "media_repackage":
            if report.get("status") != "technical_checks_passed":
                raise ValueError("封装报告尚未通过")
            outputs = {"final_media": report["output"]["path"], "technical_report": row["result_path"]}
        else:
            if report.get("technical_validation", {}).get("status") != "passed":
                raise ValueError("技术检查尚未通过")
            outputs = report["outputs"]
        if not outputs or any(not isinstance(v, str) or not Path(v).is_file() for v in outputs.values()):
            raise ValueError("完成报告缺少实际产物")
        return outputs

    def _finish(self, row, code):
        terminal = row.get("terminal_event")
        if terminal == "job_completed" and code in {0, None}:
            try:
                row["outputs"] = self._success_outputs(row)
                row.update(status="completed", progress=100, message="处理完成；请检查实际产物")
                if row.get("warning_count"):
                    row["message"] = "处理完成，有提示可查看；成品已保留"
            except PermissionError:
                raise  # A brief file-sharing conflict is retried by the next poll.
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row.update(status="failed", message="完成报告或产物不完整；请查看任务记录")
                row.update(detail=self._public_text(exc), error_code=type(exc).__name__,
                           recovery_hint="确认输出文件仍在原位置，再点击重试；已有检查点会由原流程核对后复用")
        elif terminal == "job_cancelled" and code in {2, None}:
            row.update(status="stopped", message="已停止并保留进度，可继续原任务")
        else:
            row.update(status="interrupted" if code is None else "failed",
                       message="任务未完成；现场已保留，可查看正式记录后重试")
        row["finished_at"] = time.time()
        row["exit_code"] = code
        if row["status"] == "completed":
            self.credentials.pop(row["id"], None)
        self.processes.pop(row["id"], None)
        # Only a real transition, not historical list refresh. Best-effort and keyless.
        try:
            self.notifier(row["status"])
        except Exception:
            pass

    def tick(self):
        with self.lock:
            active = [r for r in self.state["items"] if r["status"] in ACTIVE]
            changed = bool(self.state.get("error"))
            for row in active:
                if row["status"] == "stopping":
                    # Backends clear the previous run's marker during startup.
                    self._signal_stop(row)
                changed |= self._read_events(row)
                process = self.processes.get(row["id"])
                if process is not None:
                    code = process.poll()
                    alive = code is None
                else:
                    stamp = process_stamp(row.get("child_pid", 0))
                    alive = bool(stamp and stamp == row.get("child_stamp"))
                    code = None
                if not alive:
                    self._read_events(row)
                    self._finish(row, code)
                    changed = True
            if not any(r["status"] in ACTIVE for r in self.state["items"]) and not self.state["paused"]:
                row = next((r for r in self.state["items"] if r["status"] == "queued"), None)
                if row:
                    launching = False
                    try:
                        if request_info(row["request_path"])["request_digest"] != row["request_digest"]:
                            raise ValueError("排队后的任务设置已改变，请重新选择文件和设置再加入")
                        path = Path(row["request_path"]).parent / "events.jsonl"
                        row.update(status="starting", message="正在启动", started_at=time.time(),
                                   event_offset=path.stat().st_size if path.exists() else 0,
                                   last_seq=0, terminal_event=None, outputs={}, child_pid=0, child_stamp=None,
                                   detail="", error_code="", recovery_hint="", warning_count=0, last_warning="")
                        self._save()
                        launching = True
                        process = self.launcher(row, self.credentials.get(row["id"], ""))
                        self.processes[row["id"]] = process
                        row.update(status="running", child_pid=process.pid, message="正在处理")
                        try:
                            row["child_stamp"] = process_stamp(process.pid)
                        except OSError:
                            pass  # This worker still owns and polls the Popen handle.
                    except Exception as exc:
                        if isinstance(exc, PermissionError) and not launching:
                            row.update(status="queued", message="任务文件暂时被占用，正在自动重试")
                            raise
                        row.update(status="failed", message="无法启动；请核对原请求、工具路径和任务记录")
                        row.update(detail=self._public_text(exc), error_code=type(exc).__name__,
                                   recovery_hint="确认文件仍在原位置、工具路径可用，再点击重试；其他任务会继续")
                    changed = True
            if changed:
                self.state.pop("error", None)
                self._save()

    def run(self):
        while not self.closed:
            interval = 0.25
            try:
                self.tick()
            except OSError:
                # Transient sharing/disk errors must not change the user's pause
                # choice. Continue the same operation once storage is available.
                with self.lock:
                    self.state["error"] = "暂时无法读写任务记录，正在自动重试；若持续出现，请检查磁盘空间和目录权限"
                interval = 1.0
            except Exception:
                # Do not launch further work after a persistence/control failure.
                with self.lock:
                    self.state["paused"] = True
                    self.state["error"] = "后台记录暂时无法更新；当前任务保留，请检查磁盘和任务记录"
            self.wake.wait(interval)
            self.wake.clear()
