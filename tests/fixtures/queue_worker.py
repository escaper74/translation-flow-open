"""Offline subprocess fixture. Never invokes a provider or media executable."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from translation_flow.events import EventEmitter
from translation_flow.queue_cli import serve
from translation_flow.task_queue import write_json


def launch(row, key):
    env = dict(os.environ)
    env["QUEUE_TEST_KEY"] = key
    return subprocess.Popen([sys.executable, __file__, "job", row["request_path"]],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def job(path):
    root = path.parent
    payload = json.loads(path.read_text(encoding="utf-8"))
    emitter = EventEmitter(payload["job_id"], root / "events.jsonl")
    marker = root / "cancel.requested"
    marker.unlink(missing_ok=True)
    emitter.emit("job_started")
    if "fail" in payload["job_id"] and not (root / "failed.once").exists():
        (root / "failed.once").write_text("fixture")
        emitter.emit("job_failed", message="offline fixture failure")
        return 1
    for number in range(10):
        if marker.exists():
            emitter.emit("job_cancelled")
            return 2
        checkpoint = root / f"batch-{number}.fixture"
        reused = checkpoint.exists()
        if not reused:
            time.sleep(0.12)
            checkpoint.write_text("completed once", encoding="utf-8")
        emitter.emit("stage_progress", stage="translate_initial", overall_percent=(number + 1) * 9,
                     resumed=reused)
    output = root / "fixture.zh.srt"
    output.write_text("1\n00:00:00,000 --> 00:00:01,000\n离线字幕\n", encoding="utf-8")
    report = root / "translation.report.json"
    write_json(report, {"fixture": True})
    result = {"technical_validation": {"status": "passed"},
              "outputs": {"chinese_srt": str(output), "technical_report": str(report)}}
    write_json(root / "job.result.json", result)
    emitter.emit("job_completed", **result)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(Path(sys.argv[2]), launcher=launch)
    else:
        raise SystemExit(job(Path(sys.argv[2])))
