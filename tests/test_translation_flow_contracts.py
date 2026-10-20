from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL, JobRequest
from translation_flow.events import EventEmitter


class JobRequestTests(unittest.TestCase):
    def test_rejects_retired_or_unknown_deepseek_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mkv"
            source.write_bytes(b"source")
            payload = {
                "schema_version": 1,
                "job_id": "job-model",
                "input_video": str(source),
                "output_dir": directory,
                "profile_id": "normal",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": "deepseek-chat",
                },
                "consent": {
                    "current_run": True,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
            }
            with self.assertRaisesRegex(ValueError, "V4"):
                JobRequest.from_mapping(payload)

    def test_rejects_empty_output_and_path_like_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mkv"
            source.write_bytes(b"source")
            base = {
                "schema_version": 1,
                "job_id": "../escape",
                "input_video": str(source),
                "output_dir": "",
                "profile_id": "normal",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": DEFAULT_DEEPSEEK_MODEL,
                },
                "consent": {
                    "current_run": True,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
            }
            with self.assertRaisesRegex(ValueError, "job_id"):
                JobRequest.from_mapping(base)
            base["job_id"] = "job-1"
            with self.assertRaisesRegex(ValueError, "输出目录"):
                JobRequest.from_mapping(base)

    def test_requires_current_run_privacy_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mkv"
            source.write_bytes(b"source")
            payload = {
                "schema_version": 1,
                "job_id": "job-1",
                "input_video": str(source),
                "output_dir": directory,
                "profile_id": "normal",
                "provider": {
                    "id": "deepseek",
                    "host": "api.deepseek.com",
                    "model": DEFAULT_DEEPSEEK_MODEL,
                },
                "consent": {
                    "current_run": False,
                    "subtitle_text_only": True,
                    "audio_video_local": True,
                },
            }
            with self.assertRaisesRegex(ValueError, "尚未确认"):
                JobRequest.from_mapping(payload)

    def test_accepts_the_two_explicit_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mkv"
            source.write_bytes(b"source")
            for profile in ("normal", "adult_direct"):
                with self.subTest(profile=profile):
                    request = JobRequest.from_mapping(
                        {
                            "schema_version": 1,
                            "job_id": "job-1",
                            "input_video": str(source),
                            "output_dir": directory,
                            "profile_id": profile,
                            "provider": {
                                "id": "deepseek",
                                "host": "api.deepseek.com",
                                "model": DEFAULT_DEEPSEEK_MODEL,
                            },
                            "consent": {
                                "current_run": True,
                                "subtitle_text_only": True,
                                "audio_video_local": True,
                            },
                        }
                    )
                    self.assertEqual(profile, request.profile_id)


class EventEmitterTests(unittest.TestCase):
    def test_stdout_and_job_record_receive_the_same_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            path = Path(directory) / "events.jsonl"
            emitter = EventEmitter("job-1", path, output)
            emitter.emit("stage_started", stage="asr_a")
            stdout_item = json.loads(output.getvalue())
            file_item = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stdout_item, file_item)
            self.assertEqual(1, file_item["seq"])
            self.assertEqual("job-1", file_item["job_id"])

    def test_same_job_retry_continues_event_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            EventEmitter("job-1", path, io.StringIO()).emit("job_failed")
            output = io.StringIO()
            EventEmitter("job-1", path, output).emit("job_started")

            self.assertEqual(2, json.loads(output.getvalue())["seq"])
            self.assertEqual(
                [1, 2],
                [json.loads(line)["seq"] for line in path.read_text(encoding="utf-8").splitlines()],
            )


if __name__ == "__main__":
    unittest.main()
