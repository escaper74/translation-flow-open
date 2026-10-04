from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from translation_flow.events import EventEmitter
from translation_flow.queue_cli import ROOT, rpc, run_request
from translation_flow.task_queue import FileLease, TaskQueue, process_stamp, request_info, write_json


def make_request(root, name="one", provider="codex"):
    source = root / f"{name}.ja.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nこんにちは。\n", encoding="utf-8")
    path = root / ".srt-translation" / name / "request.json"
    write_json(path, {"schema_version": 1, "workflow_mode": "srt_translation", "job_id": name,
                     "input_srt": str(source), "output_dir": str(root), "profile_id": "normal",
                     "provider": {"id": provider, "host": "chatgpt.com" if provider == "codex" else "api.deepseek.com",
                                  "model": "cli-default" if provider == "codex" else "deepseek-v4-pro"},
                     "consent": {"current_run": True, "subtitle_text_only": True,
                                 "audio_video_local": True, "provider": provider}})
    return path


def emit(path, event, **kwargs):
    EventEmitter(path.parent.name, path.parent / "events.jsonl", stdout=io.StringIO()).emit(event, **kwargs)


def complete(path):
    output = path.parent / "test.zh.srt"
    output.write_text("fixture", encoding="utf-8")
    write_json(path.parent / "job.result.json", {"technical_validation": {"status": "passed"},
                                                "outputs": {"chinese_srt": str(output)}})
    emit(path, "job_completed")


class Process:
    pid = os.getpid()
    code = None

    def poll(self):
        return self.code


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.launched = []

        def launch(row, key):
            process = Process()
            self.launched.append((row["job_id"], key, process))
            return process

        self.queue = TaskQueue(self.root / "queue", launcher=launch)

    def enqueue(self, *paths, key=""):
        return self.queue.command({"action": "enqueue", "authorized": True,
                                   "items": [{"request_path": str(p), "api_key": key} for p in paths]})

    def test_repeated_clicks_and_duplicate_batch_entries_launch_once(self):
        path = make_request(self.root)
        self.enqueue(path, path)
        self.enqueue(path)
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(1, len(self.queue.snapshot()["items"]))
        self.assertEqual(1, len(self.launched))

    def test_batch_skips_unusable_input_but_requires_current_consent(self):
        path = make_request(self.root)
        with self.assertRaises(ValueError):
            self.queue.command({"action": "enqueue", "items": [{"request_path": str(path)}]})
        self.assertEqual([], self.queue.snapshot()["items"])
        result = self.enqueue(path, self.root / "missing.json")
        self.assertEqual(1, len(result["items"]))
        self.assertEqual(1, len(result["submission"]["skipped"]))
        self.queue.tick()
        self.assertEqual(["one"], [x[0] for x in self.launched])

    def test_paused_or_stopped_task_reuses_key_within_same_worker(self):
        for started in (False, True):
            with self.subTest(started=started):
                path = make_request(self.root, f"session-{started}", provider="deepseek")
                result = self.enqueue(path, key="session-key-fixture")
                row_id = result["items"][-1]["id"]
                if started:
                    self.queue.tick()
                self.queue.command({"action": "stop", "id": row_id})
                if started:
                    emit(path, "job_cancelled")
                    self.launched[-1][2].code = 2
                    self.queue.tick()
                self.assertTrue(self.queue.snapshot()["items"][-1]["has_session_key"])
                self.queue.command({"action": "resume", "ids": [row_id], "authorized": True,
                                    "credentials": {row_id: ""}})
                self.queue.tick()
                self.assertEqual("session-key-fixture", self.launched[-1][1])
                complete(path)
                self.launched[-1][2].code = 0
                self.queue.tick()
                self.assertFalse(self.queue.snapshot()["items"][-1]["has_session_key"])
        self.assertNotIn("session-key-fixture", self.queue.path.read_text(encoding="utf-8"))

    def test_resuming_can_replace_a_session_key_without_persisting_it(self):
        path = make_request(self.root, provider="deepseek")
        row_id = self.enqueue(path, key="old-key-fixture")["items"][0]["id"]
        self.queue.command({"action": "pause"})
        self.queue.command({"action": "resume", "ids": [row_id], "authorized": True,
                            "credentials": {row_id: "new-key-fixture"}})
        self.queue.tick()
        self.assertEqual("new-key-fixture", self.launched[0][1])
        self.assertNotIn("key-fixture", self.queue.path.read_text(encoding="utf-8"))

    def test_temporary_event_read_error_does_not_leave_queue_paused(self):
        path = make_request(self.root)
        self.enqueue(path)
        self.queue.tick()
        waits = []

        def wait(interval):
            waits.append(interval)
            if len(waits) == 2:
                self.queue.closed = True

        with patch.object(self.queue, "_read_events", side_effect=[PermissionError("busy"), False]), \
             patch.object(self.queue.wake, "wait", side_effect=wait):
            self.queue.run()
        self.assertEqual([1.0, 0.25], waits)
        self.assertFalse(self.queue.snapshot()["paused"])
        self.assertNotIn("error", self.queue.snapshot())
        self.assertEqual(1, len(self.launched))

    def test_locked_completion_report_is_retried_without_rerunning_task(self):
        path = make_request(self.root)
        self.enqueue(path)
        self.queue.tick()
        complete(path)
        self.launched[0][2].code = 0
        with patch.object(self.queue, "_success_outputs", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.queue.tick()
        self.assertEqual("running", self.queue.snapshot()["items"][0]["status"])
        self.queue.tick()
        self.assertEqual("completed", self.queue.snapshot()["items"][0]["status"])
        self.assertEqual(1, len(self.launched))

    def test_locked_queue_record_retries_but_denied_launch_does_not_block_next_job(self):
        a, b = make_request(self.root, "a"), make_request(self.root, "b")
        self.enqueue(a, b)
        with patch.object(self.queue, "_save", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.queue.tick()
        self.assertEqual("queued", self.queue.snapshot()["items"][0]["status"])
        self.assertEqual([], self.launched)
        with patch.object(self.queue, "launcher", side_effect=PermissionError("execution denied")):
            self.queue.tick()
        row = self.queue.snapshot()["items"][0]
        self.assertEqual("failed", row["status"])
        self.assertIn("execution denied", row["detail"])
        self.queue.tick()
        self.assertEqual(["b"], [x[0] for x in self.launched])

    def test_failure_reason_and_recovery_hint_reach_client_without_key(self):
        path = make_request(self.root, provider="deepseek")
        self.enqueue(path, key="private-key-fixture")
        self.queue.tick()
        emit(path, "job_failed", message="Connection lost private-key-fixture", error_code="ConnectionError",
             recovery_hint="稍后点击重试，成功批次会继续复用")
        self.launched[0][2].code = 1
        self.queue.tick()
        row = self.queue.snapshot()["items"][0]
        self.assertIn("Connection lost", row["detail"])
        self.assertEqual("ConnectionError", row["error_code"])
        self.assertIn("点击重试", row["recovery_hint"])
        self.assertNotIn("private-key-fixture", json.dumps(row))
        self.assertNotIn("private-key-fixture", self.queue.path.read_text(encoding="utf-8"))

    def test_warning_does_not_pause_or_turn_valid_output_into_failure(self):
        path = make_request(self.root)
        self.enqueue(path)
        self.queue.tick()
        emit(path, "warning", message="一条字幕较长，建议观看时检查")
        complete(path)
        self.launched[0][2].code = 0
        self.queue.tick()
        row = self.queue.snapshot()["items"][0]
        self.assertEqual("completed", row["status"])
        self.assertFalse(self.queue.snapshot()["paused"])
        self.assertEqual(1, row["warning_count"])
        self.assertIn("字幕较长", row["last_warning"])

    def test_queue_runs_sequentially_and_one_failure_does_not_stop_next_item(self):
        a, b = make_request(self.root, "a"), make_request(self.root, "b")
        self.enqueue(a, b)
        self.queue.tick()
        self.assertEqual(["a"], [x[0] for x in self.launched])
        emit(a, "job_failed")
        self.launched[0][2].code = 1
        self.queue.tick()
        self.assertEqual(["a", "b"], [x[0] for x in self.launched])
        complete(b)
        self.launched[1][2].code = 0
        self.queue.tick()
        self.assertEqual(["failed", "completed"], [r["status"] for r in self.queue.snapshot()["items"]])

    def test_stop_all_preserves_pending_and_reasserts_signal_after_startup_clear(self):
        a, b = make_request(self.root, "a"), make_request(self.root, "b")
        self.enqueue(a, b)
        self.queue.tick()
        self.queue.command({"action": "stop"})
        signal = a.parent / "cancel.requested"
        signal.unlink()
        self.queue.tick()
        self.assertTrue(signal.exists())
        emit(a, "job_cancelled")
        self.launched[0][2].code = 2
        self.queue.tick()
        self.assertEqual(["stopped", "paused"], [r["status"] for r in self.queue.snapshot()["items"]])
        self.assertEqual(1, len(self.launched))

    def test_restart_keeps_queue_but_never_persists_keys_or_reauthorizes(self):
        path = make_request(self.root, provider="deepseek")
        self.enqueue(path, key="secret-only-for-offline-test")
        self.assertNotIn("secret-only", self.queue.path.read_text(encoding="utf-8"))
        restored = TaskQueue(self.root / "queue", launcher=lambda *_: self.fail("must not launch"))
        restored.tick()
        row = restored.snapshot()["items"][0]
        self.assertEqual("paused", row["status"])
        self.assertEqual({}, restored.credentials)
        with self.assertRaises(ValueError):
            restored.command({"action": "resume", "ids": [row["id"]]})

    def test_enqueue_while_paused_is_actionable_and_resume_selects_only_new_batch(self):
        old, new = make_request(self.root, "old"), make_request(self.root, "new")
        self.queue.command({"action": "pause"})
        old_id = self.enqueue(old)["submission"]["added"][0]
        result = self.enqueue(new)
        self.assertTrue(result["paused"])
        self.assertEqual(["paused", "paused"], [row["status"] for row in result["items"]])
        self.queue.tick()
        self.assertEqual([], self.launched)
        # Exercise old on-disk records as well: a globally paused item used to
        # have status queued. Selecting the new item cannot release the old one.
        self.queue._row(old_id)["status"] = "queued"
        self.queue.command({"action": "resume", "ids": result["submission"]["added"], "authorized": True})
        self.queue.tick()
        self.assertEqual(["new"], [entry[0] for entry in self.launched])
        self.assertEqual("paused", self.queue._row(old_id)["status"])

    def test_request_change_after_enqueue_cannot_change_provider_silently(self):
        path = make_request(self.root)
        self.enqueue(path)
        raw = json.loads(path.read_text())
        raw["profile_id"] = "adult_direct"
        write_json(path, raw)
        self.queue.tick()
        self.assertEqual([], self.launched)
        self.assertEqual("failed", self.queue.snapshot()["items"][0]["status"])

    def test_old_success_or_zero_exit_without_new_valid_result_is_not_completion(self):
        path = make_request(self.root)
        complete(path)
        self.enqueue(path)
        self.queue.tick()
        self.launched[0][2].code = 0
        self.queue.tick()
        self.assertEqual("failed", self.queue.snapshot()["items"][0]["status"])

    def test_completion_requires_outputs_and_stop_after_completion_preserves_success(self):
        path = make_request(self.root)
        self.enqueue(path)
        self.queue.tick()
        complete(path)
        self.queue.command({"action": "stop"})
        self.launched[0][2].code = 0
        self.queue.tick()
        self.assertEqual("completed", self.queue.snapshot()["items"][0]["status"])
        (path.parent / "test.zh.srt").unlink()
        with self.assertRaises(ValueError):
            self.queue._success_outputs(self.queue.state["items"][0])

    def test_request_lease_blocks_a_duplicate_child_before_backend_entry(self):
        path = make_request(self.root)
        with FileLease(path.parent / "queue.run.lock"), patch("translation_flow.queue_cli.importlib.import_module") as module:
            self.assertEqual(1, run_request(path, request_info(path)["request_digest"], ""))
            module.assert_not_called()

    def test_queue_rejects_unknown_request_fields_and_modes(self):
        path = make_request(self.root)
        raw = json.loads(path.read_text())
        raw["api_key"] = "fixture"
        write_json(path, raw)
        with self.assertRaises(ValueError):
            self.enqueue(path)
        raw.pop("api_key")
        raw["workflow_mode"] = "unknown"
        write_json(path, raw)
        with self.assertRaises(ValueError):
            self.enqueue(path)


class BackgroundQueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "任务队列 with space"
        self.workers = []
        self.worker = self.start_worker()

    def start_worker(self):
        worker = subprocess.Popen([sys.executable, str(ROOT / "tests/fixtures/queue_worker.py"), "serve", str(self.state)],
                                  cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.workers.append(worker)
        self.wait_for(lambda: rpc(self.state, {"action": "status"}), timeout=5)
        return worker

    def wait_for(self, callback, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                value = callback()
                if value:
                    return value
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.05)
        self.fail("offline background fixture timed out")

    def status(self):
        return rpc(self.state, {"action": "status"})["queue"]

    def tearDown(self):
        try:
            rpc(self.state, {"action": "stop"})
            self.wait_for(lambda: not any(r["status"] in {"starting", "running", "stopping"} for r in self.status()["items"]))
            rpc(self.state, {"action": "shutdown"})
        finally:
            for worker in self.workers:
                try:
                    worker.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    worker.kill()  # Only the fixture process created by this test.
                    worker.wait(timeout=4)
            self.temp.cleanup()

    def test_short_lived_client_exit_stop_resume_and_batch_failure(self):
        a, b = make_request(self.root, "a"), make_request(self.root, "fail-once")
        payload = {"action": "enqueue", "authorized": True,
                   "items": [{"request_path": str(p)} for p in (a, b)]}
        client = subprocess.run([sys.executable, "-m", "translation_flow.queue_cli", "--state-dir", str(self.state)],
                                input=json.dumps(payload), cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        self.assertEqual(0, client.returncode, client.stdout + client.stderr)
        self.wait_for(lambda: self.status()["items"][0]["progress"] > 0)
        pid = self.status()["worker_pid"]
        self.assertEqual(self.worker.pid, pid)
        rpc(self.state, {"action": "stop"})
        self.wait_for(lambda: self.status()["items"][0]["status"] == "stopped")
        first_batch = a.parent / "batch-0.fixture"
        stamp = first_batch.stat().st_mtime_ns
        ids = [r["id"] for r in self.status()["items"]]
        self.assertTrue(rpc(self.state, {"action": "resume", "ids": ids, "authorized": True})["ok"])
        self.wait_for(lambda: [r["status"] for r in self.status()["items"]] == ["completed", "failed"])
        self.assertEqual(stamp, first_batch.stat().st_mtime_ns)
        rpc(self.state, {"action": "resume", "ids": [ids[1]], "authorized": True})
        self.wait_for(lambda: all(r["status"] == "completed" for r in self.status()["items"]))
        self.assertEqual(pid, self.status()["worker_pid"])

    def test_authentication_and_duplicate_worker_protection(self):
        endpoint = json.loads((self.state / "worker.json").read_text())
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                urllib.request.Request(f"http://127.0.0.1:{endpoint['port']}/rpc", data=b'{}'), timeout=2)
        self.assertEqual(403, error.exception.code)
        duplicate = subprocess.run([sys.executable, str(ROOT / "tests/fixtures/queue_worker.py"), "serve", str(self.state)],
                                   cwd=ROOT, capture_output=True, timeout=5)
        self.assertNotEqual(0, duplicate.returncode)
        self.assertEqual(self.worker.pid, self.status()["worker_pid"])

    def test_production_client_starts_empty_detached_worker_and_reconnects(self):
        state = self.root / "empty production worker"
        pid = None
        try:
            results = []
            for _ in range(2):
                result = subprocess.run([sys.executable, "-m", "translation_flow.queue_cli", "--state-dir", str(state)],
                                        input='{"action":"status"}', cwd=ROOT, capture_output=True,
                                        text=True, encoding="utf-8", timeout=10)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                reply = json.loads(result.stdout)
                self.assertTrue(reply["ok"])
                self.assertEqual([], reply["queue"]["items"])
                pid = reply["queue"]["worker_pid"]
                results.append(pid)
            self.assertEqual(results[0], results[1])
        finally:
            if pid:
                rpc(state, {"action": "shutdown"})
                self.wait_for(lambda: process_stamp(pid) is None)

    def test_production_worker_reuses_formal_srt_without_api_key(self):
        import test_srt_desktop as desktop_fixture
        source_root = self.root / "cached subtitle"
        source_root.mkdir()
        helper = desktop_fixture.SrtDesktopTests()
        path, _ = helper.request(source_root, "deepseek")
        status, _, provider = helper.call(path)  # Fake provider only; no HTTP.
        self.assertEqual(0, status)
        state = self.root / "production cached queue"
        pid = None
        try:
            payload = {"action": "enqueue", "authorized": True, "items": [{"request_path": str(path)}]}
            client = subprocess.run([sys.executable, "-m", "translation_flow.queue_cli", "--state-dir", str(state)],
                                    input=json.dumps(payload), cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", timeout=10)
            self.assertEqual(0, client.returncode, client.stdout + client.stderr)
            pid = json.loads(client.stdout)["queue"]["worker_pid"]
            def finished():
                snapshot = rpc(state, {"action": "status"})["queue"]
                return snapshot if snapshot["items"][0]["status"] in {"completed", "failed"} else None
            snapshot = self.wait_for(finished)
            self.assertEqual("completed", snapshot["items"][0]["status"], snapshot)
            self.assertNotIn("SUBTITLE_API_KEY", (state / "queue.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (path.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(events[-1]["reused"])
        finally:
            if pid:
                rpc(state, {"action": "stop"})
                self.wait_for(lambda: not any(r["status"] in {"starting", "running", "stopping"}
                                             for r in rpc(state, {"action": "status"})["queue"]["items"]))
                rpc(state, {"action": "shutdown"})
                self.wait_for(lambda: process_stamp(pid) is None)

    def test_worker_crash_reattaches_existing_child_without_launching_pending(self):
        a, b = make_request(self.root, "a"), make_request(self.root, "b")
        rpc(self.state, {"action": "enqueue", "authorized": True,
                         "items": [{"request_path": str(p)} for p in (a, b)]})
        self.wait_for(lambda: self.status()["items"][0]["progress"] > 0)
        original_pid = self.status()["items"][0]["child_pid"]
        self.worker.kill()
        self.worker.wait(timeout=4)
        self.worker = self.start_worker()
        self.assertEqual(original_pid, self.status()["items"][0]["child_pid"])
        self.wait_for(lambda: self.status()["items"][0]["status"] == "completed")
        self.assertEqual("paused", self.status()["items"][1]["status"])
        self.assertFalse((b.parent / "events.jsonl").exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell bridge")
    def test_powershell_bridge_can_reconnect_and_preserves_chinese_paths(self):
        self.check_powershell_bridge_status()

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell bridge")
    def test_powershell_bridge_accepts_utf8_console_input_preamble(self):
        # The formal .cmd uses chcp 65001. Framework's redirected stdin writer
        # then emits a BOM, even when the bridge writes through BaseStream.
        self.check_powershell_bridge_status(utf8_console=True)

    def check_powershell_bridge_status(self, *, utf8_console=False):
        script = self.root / "bridge-test.ps1"
        script.write_text(
            "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)\n"
            + ("[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($true)\n" if utf8_console else "")
            + f". '{ROOT / 'desktop/TranslationFlowQueueBridge.ps1'}'\n"
            f"try {{ $q=Invoke-TranslationFlowQueue -PythonPath '{sys.executable}' -WorkingDirectory '{ROOT}' "
            f"-StateDirectory '{self.state}' -Payload @{{action='status'}}\n"
            "} catch { [Console]::Error.WriteLine($_.ScriptStackTrace); throw }\n"
            "$q | ConvertTo-Json -Depth 10 -Compress", encoding="utf-8-sig")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                                env={key.upper(): value for key, value in os.environ.items()})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(self.worker.pid, json.loads(result.stdout)["worker_pid"])


if __name__ == "__main__":
    unittest.main()
