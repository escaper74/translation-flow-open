"""WPF integration fixture: local text checkpoints, no translation or media tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from translation_flow.events import EventEmitter
from translation_flow.queue_cli import serve
from translation_flow.task_queue import write_json


def launch(row, key):
    return subprocess.Popen([sys.executable, __file__, "job", row["request_path"]],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def job(path):
    request = json.loads(path.read_text(encoding="utf-8"))
    job_dir = path.parent
    signal = job_dir / "cancel.requested"
    signal.unlink(missing_ok=True)
    events = EventEmitter(request["job_id"], job_dir / "events.jsonl")
    events.emit("job_started")
    events.emit("stage_started", stage="translate_initial", overall_percent=10)
    if Path(request["input_srt"]).stem == "fail":
        events.emit("job_failed", message="offline fixture failure", error_code="FixtureFailure",
                    recovery_hint="测试失败项已保留，后续项继续")
        return 1
    for number in range(12):
        if signal.exists():
            events.emit("job_cancelled", message="fixture stopped")
            return 2
        checkpoint = job_dir / f"part-{number}.fixture"
        reused = checkpoint.exists()
        if not reused:
            time.sleep(0.3)
            checkpoint.write_text("local fixture", encoding="utf-8")
        events.emit("stage_progress", stage="translate_initial", detail=f"{number+1}/12",
                    resumed=reused, overall_percent=10+(number+1)*7)
    output = job_dir / "fixture.zh-CN.srt"
    output.write_text("1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")
    report = job_dir / "translation.report.json"
    write_json(report, {"fixture": True, "status": "passed"})
    result = {"outputs": {"chinese_srt": str(output), "technical_report": str(report)},
              "technical_validation": {"status": "passed", "report_path": str(report)},
              "stages": {"translation": {"reused": False}}}
    write_json(job_dir / "job.result.json", result)
    events.emit("job_completed", **result)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(Path(sys.argv[2]), launcher=launch)
    else:
        raise SystemExit(job(Path(sys.argv[2])))
