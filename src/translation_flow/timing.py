"""Small derived timing report. Events remain the source of truth."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Iterable


TERMINAL_EVENTS = {"job_completed", "job_failed", "job_cancelled"}


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    attempt: dict[str, Any] | None = None
    starts: dict[str, tuple[datetime, dict[str, Any]]] = {}
    beginning: datetime | None = None
    for event in events:
        timestamp = datetime.fromisoformat(event["timestamp_utc"])
        kind = event.get("event")
        if kind == "job_started" or attempt is None:
            if attempt is not None and attempt["terminal_event"] is None:
                attempt["unfinished_stages"] = list(starts)
            beginning = timestamp
            attempt = {"started_at": event["timestamp_utc"], "wall_seconds": None,
                       "job_start_observed": kind == "job_started",
                       "terminal_event": None, "stages": [], "unfinished_stages": []}
            attempts.append(attempt)
            starts = {}
        stage = event.get("stage")
        if kind == "stage_started" and stage:
            starts[stage] = (timestamp, event)
        elif kind in {"stage_completed", "stage_failed"} and stage:
            started = starts.pop(stage, None)
            attempt["stages"].append({
                "stage": stage, "status": kind.removeprefix("stage_"),
                "wall_seconds": max(0, (timestamp - started[0]).total_seconds()) if started else None,
                "resumed": event.get("resumed", started[1].get("resumed", False) if started else False),
                "started_at": started[1]["timestamp_utc"] if started else None,
                "ended_at": event["timestamp_utc"],
            })
        if kind in TERMINAL_EVENTS:
            attempt["terminal_event"] = kind
            attempt["ended_at"] = event["timestamp_utc"]
            attempt["wall_seconds"] = (max(0, (timestamp - beginning).total_seconds())
                                       if attempt["job_start_observed"] else None)
            attempt["unfinished_stages"] = list(starts)
    return {"schema_version": 1, "attempts": attempts,
            "completed_attempt_wall_seconds": sum(a["wall_seconds"] or 0 for a in attempts),
            "notes": ["Parallel stage durations must not be added to obtain wall time.",
                      "Wall time excludes gaps between separate attempts; resumed stages are labelled.",
                      "Unknown/missing stage starts have null duration, not an estimated zero."]}


def write_timing_summary(events_path: Path, job_id: str) -> Path:
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("job_id") == job_id and event.get("timestamp_utc"):
            events.append(event)
    summary = summarize_events(events)
    summary["job_id"] = job_id
    summary["events_path"] = str(events_path)
    translation_report = events_path.parent / "translation.report.json"
    if translation_report.is_file():
        report = json.loads(translation_report.read_text(encoding="utf-8-sig"))
        phases = report.get("phase_wall_seconds")
        if isinstance(phases, dict):
            summary["translation_report_phases"] = {
                "source": str(translation_report), "seconds": phases,
                "note": "Report measurements; may originate from a previous attempt when translation is reused.",
            }
    target = events_path.parent / "timing.summary.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target
