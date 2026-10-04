from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class EventEmitter:
    """把同一份 JSONL 事件写到 stdout 和任务记录。"""

    def __init__(self, events_path: Path, stdout: TextIO) -> None:
        self.events_path = events_path
        self.stdout = stdout
        self._lock = threading.Lock()
        events_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        item = {
            "event": event,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
            try:
                self.stdout.write(line + "\n")
            except UnicodeEncodeError:
                # 老 Windows 控制台可能仍是 CP936。ASCII 转义后的内容仍是同一份 JSON，
                # 不能让一次进度输出破坏正在运行的 GPU 子进程。
                fallback = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
                self.stdout.write(fallback + "\n")
            self.stdout.flush()
        return item
