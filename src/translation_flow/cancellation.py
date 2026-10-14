"""Stop owned ASR processes promptly; preserve completed translation batches."""

from contextlib import contextmanager
import os
import signal
import subprocess
import threading
import time

from .translation.codex_provider import TranslationCancelled


def check_cancelled(cancelled):
    if cancelled():
        raise TranslationCancelled("已停止并保留成功批次与检查点；用同一请求继续。")


def run_asr_process(args, *, cancelled, env=None):
    """Drain output while polling stop; only terminate this invocation's tree."""
    check_cancelled(cancelled)
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        start_new_session=os.name != "nt",
    ) as process:
        try:
            while True:
                check_cancelled(cancelled)
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
            check_cancelled(cancelled)
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    # A owns whisper-cli/FFmpeg grandchildren. Killing only the
                    # Python wrapper would leave their CPU/GPU load running.
                    stopped = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=5,
                    )
                    if stopped.returncode and process.poll() is None:
                        raise RuntimeError("无法停止本次 ASR 子进程树；尚未确认负载退出。")
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.communicate(timeout=5)
            raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


@contextmanager
def interrupts(enabled=True):
    stopped = threading.Event()
    install = enabled and threading.current_thread() is threading.main_thread()
    if install:
        previous = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        yield stopped.is_set
    finally:
        if install:
            signal.signal(signal.SIGINT, previous)


class CancellableProvider:
    """Let completed HTTP responses reach their cache; stop the next call."""
    def __init__(self, wrapped, cancelled):
        self.wrapped = wrapped
        self.cancelled = cancelled

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def check_cancelled(self):
        check_cancelled(self.cancelled)

    def chat(self, *args, **kwargs):
        self.check_cancelled()
        return self.wrapped.chat(*args, **kwargs)

    def check_connection(self):
        self.check_cancelled()
        self.wrapped.check_connection()
        self.check_cancelled()

    def wait_retry(self, seconds):
        remaining = float(seconds)
        while remaining > 0:
            self.check_cancelled()
            interval = min(1.0, remaining)
            time.sleep(interval)
            remaining -= interval
        self.check_cancelled()


class StopAwareEvents:
    def __init__(self, events, cancelled):
        self.events, self.cancelled = events, cancelled

    def emit(self, event, **payload):
        if event == "stage_started":
            check_cancelled(self.cancelled)
        self.events.emit(event, **payload)
