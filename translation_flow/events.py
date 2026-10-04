from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .contracts import SCHEMA_VERSION


class EventEmitter:
    """Write the same UTF-8 JSONL event stream to stdout and the job record."""

    def __init__(
        self,
        job_id: str,
        events_path: Path,
        stdout: TextIO | None = None,
    ) -> None:
        self.job_id = job_id
        self.events_path = events_path
        self.stdout = stdout or sys.stdout
        self._seq = self._last_sequence(events_path, job_id)
        self._lock = threading.Lock()
        events_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _last_sequence(events_path: Path, job_id: str) -> int:
        if not events_path.is_file():
            return 0
        for line in reversed(events_path.read_text(encoding="utf-8").splitlines()):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            sequence = item.get("seq")
            if item.get("job_id") == job_id and isinstance(sequence, int) and sequence > 0:
                return sequence
        return 0

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            item = {
                "schema_version": SCHEMA_VERSION,
                "seq": self._seq,
                "event": event,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "job_id": self.job_id,
                **payload,
            }
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
            try:
                self.stdout.write(line + "\n")
            except UnicodeEncodeError:
                self.stdout.write(json.dumps(item, ensure_ascii=True) + "\n")
            self.stdout.flush()
            if event in {"job_completed", "job_failed", "job_cancelled"}:
                # This is derived diagnostic data, never a new completion gate.
                from .timing import write_timing_summary
                try:
                    write_timing_summary(self.events_path, self.job_id)
                except (OSError, ValueError, TypeError, KeyError):
                    sys.stderr.write("Timing summary unavailable; original events and results are retained.\n")
            return item
