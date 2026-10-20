import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from translation_flow.events import EventEmitter
from translation_flow.timing import summarize_events


def event(seconds, kind, **values):
    return {"timestamp_utc": f"2026-10-03T00:00:{seconds:02d}+00:00", "event": kind, **values}


class TimingTests(unittest.TestCase):
    def test_parallel_stages_do_not_inflate_wall(self):
        report = summarize_events([
            event(0, "job_started"), event(1, "stage_started", stage="translate"),
            event(2, "stage_started", stage="picture"), event(10, "stage_completed", stage="translate"),
            event(12, "stage_completed", stage="picture"), event(13, "job_completed"),
        ])
        self.assertEqual(13, report["completed_attempt_wall_seconds"])
        self.assertEqual([9, 10], [s["wall_seconds"] for s in report["attempts"][0]["stages"]])

    def test_resume_gap_and_unmatched_reused_stage(self):
        report = summarize_events([
            event(0, "job_started"), event(1, "stage_started", stage="A"), event(4, "job_cancelled"),
            event(40, "job_started"), event(41, "stage_completed", stage="A", resumed=True),
            event(44, "job_completed"),
        ])
        self.assertEqual(8, report["completed_attempt_wall_seconds"])
        self.assertEqual(["A"], report["attempts"][0]["unfinished_stages"])
        stage = report["attempts"][1]["stages"][0]
        self.assertIsNone(stage["wall_seconds"])
        self.assertTrue(stage["resumed"])

    def test_terminal_emitter_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emitter = EventEmitter("test", root / "events.jsonl", stdout=io.StringIO())
            emitter.emit("job_started")
            (root / "translation.report.json").write_text(json.dumps({"phase_wall_seconds": {"polish": 2}}))
            emitter.emit("job_completed")
            report = json.loads((root / "timing.summary.json").read_text())
            self.assertEqual("test", report["job_id"])
            self.assertEqual({"polish": 2}, report["translation_report_phases"]["seconds"])

    def test_missing_start_is_unknown_not_zero(self):
        report = summarize_events([event(10, "job_failed")])
        self.assertIsNone(report["attempts"][0]["wall_seconds"])
        self.assertFalse(report["attempts"][0]["job_start_observed"])

    def test_crash_restart_keeps_incomplete_attempt(self):
        report = summarize_events([event(0, "job_started"), event(1, "stage_started", stage="A"),
                                   event(40, "job_started"), event(45, "job_completed")])
        self.assertEqual(["A"], report["attempts"][0]["unfinished_stages"])
        self.assertIsNone(report["attempts"][0]["wall_seconds"])
        self.assertEqual(5, report["completed_attempt_wall_seconds"])

    def test_timing_failure_does_not_fail_completed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            emitter = EventEmitter("test", Path(directory) / "events.jsonl", stdout=io.StringIO())
            with patch("translation_flow.timing.write_timing_summary", side_effect=OSError("locked")):
                with patch("sys.stderr", new=io.StringIO()):
                    result = emitter.emit("job_completed")
            self.assertEqual("job_completed", result["event"])
