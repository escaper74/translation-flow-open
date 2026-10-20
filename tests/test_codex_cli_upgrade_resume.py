from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from translation_flow.srt import SubtitleCue, parse_srt, write_srt
from translation_flow.translation.codex_provider import (
    ADAPTER_VERSION, CodexProvider, TranslationCancelled,
)
from translation_flow.translation.pipeline import run_translation
from translation_flow.translation.provider import FatalProviderError
from translation_flow.translation.codex_provider import CodexLoginRequired
from tests.test_codex_integration import FakeCodex


def audit(path: Path, version: str, timestamp: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"adapter_version": ADAPTER_VERSION,
                               "authentication": "chatgpt",
                               "requested_model": "cli-default",
                               "cli_version": version}), encoding="utf-8")
    os.utime(path, ns=(timestamp, timestamp))


def provider(root: Path, version: str) -> CodexProvider:
    return CodexProvider(audit_dir=root, executable="codex.exe",
                         status_probe=lambda **_: {"status": "available", "message": "ready",
                                                   "cli_version": version})


class CliUpgradeResumeTests(unittest.TestCase):
    def test_removed_cli_is_rediscovered_after_asr_without_replaying_a_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "old-codex.exe"
            old.write_bytes(b"installation placeholder")
            replacement = str(root / "new-codex.exe")
            launches, sent = [], []

            class Reply:
                returncode = 0

                def __init__(self, args):
                    self.output = Path(args[args.index("--output-last-message") + 1])

                def communicate(self, input=None, timeout=None):
                    sent.append(input)
                    self.output.write_text("synthetic response", encoding="utf-8")
                    return json.dumps({"type": "turn.completed"}), ""

            def launch(args, **kwargs):
                launches.append(args[0])
                if args[0] == str(old):
                    # Exercise a real OS missing-executable error, no CLI/model.
                    return subprocess.Popen(args, **kwargs)
                return Reply(args)

            probe = Mock(side_effect=[
                {"status": "available", "cli_version": "old"},
                {"status": "available", "cli_version": "new"},
            ])
            with patch("translation_flow.translation.codex_provider.resolve_codex_executable",
                       side_effect=[str(old), replacement]):
                adapter = CodexProvider(audit_dir=root / "audit", status_probe=probe, process_factory=launch)
                identity = adapter.cache_identity
                old.unlink()  # Simulate an update during ASR.
                result = adapter.chat([{"role": "user", "content": "synthetic input"}],
                                      model="cli-default", temperature=0)
            self.assertEqual("synthetic response", result)
            self.assertEqual([str(old), replacement], launches)
            self.assertEqual(1, len(sent))
            self.assertEqual(identity, adapter.cache_identity)
            metadata = json.loads(next((root / "audit").glob("*.json")).read_text())
            self.assertEqual("new", metadata["cli_version"])
            self.assertEqual([str(old), replacement], [c.kwargs["executable"] for c in probe.call_args_list])

    def test_missing_explicit_cli_or_unchanged_resolution_does_not_retry(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as temp:
                launch = Mock(side_effect=FileNotFoundError(2, "private-path-and-input"))
                with patch("translation_flow.translation.codex_provider.resolve_codex_executable",
                           return_value="unchanged.exe") as resolve:
                    adapter = CodexProvider(audit_dir=temp, executable="unchanged.exe" if explicit else None,
                        status_probe=lambda **_: {"status": "available", "cli_version": "test"},
                        process_factory=launch)
                    with self.assertRaises(FatalProviderError) as caught:
                        adapter.chat([], model="cli-default", temperature=0)
                self.assertEqual(1, launch.call_count)
                self.assertEqual(0 if explicit else 2, resolve.call_count)
                self.assertIn("errno=2", str(caught.exception))
                self.assertNotIn("private-path-and-input", str(caught.exception))

    def test_replacement_requires_chatgpt_login_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            launch = Mock(side_effect=FileNotFoundError(2, "removed"))
            with patch("translation_flow.translation.codex_provider.resolve_codex_executable",
                       side_effect=["old.exe", "new.exe"]):
                adapter = CodexProvider(audit_dir=temp, process_factory=launch, status_probe=Mock(side_effect=[
                    {"status": "available", "cli_version": "old"},
                    {"status": "login_required", "cli_version": "new", "message": "login required"},
                ]))
                with self.assertRaises(CodexLoginRequired):
                    adapter.chat([], model="cli-default", temperature=0)
            self.assertEqual(1, launch.call_count)

    def test_file_error_after_process_started_never_redispatches(self):
        with tempfile.TemporaryDirectory() as temp:
            process = Mock(returncode=0)
            process.communicate.side_effect = [FileNotFoundError(2, "private output"), ("", "")]
            launch = Mock(return_value=process)
            with patch("translation_flow.translation.codex_provider.resolve_codex_executable",
                       return_value="current.exe") as resolve:
                adapter = CodexProvider(audit_dir=temp, process_factory=launch,
                    status_probe=lambda **_: {"status": "available", "cli_version": "test"})
                with self.assertRaises(FatalProviderError):
                    adapter.chat([], model="cli-default", temperature=0)
            self.assertEqual(1, launch.call_count)
            self.assertEqual(1, resolve.call_count)

    def test_legacy_job_reuses_context_initial_and_completed_polish_after_cli_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.srt"
            write_srt(source, [SubtitleCue(i, (i - 1) * 2000, i * 2000,
                                          f"会議の資料を確認します{i}。") for i in range(1, 5)])
            old = FakeCodex()
            old.calls = []
            old.cache_identity = f"{ADAPTER_VERSION}:chatgpt:codex-cli 0.153.4:cli-default"
            old.cancelled = lambda: len(old.calls) >= 4
            args = (source, root / "result.srt", root / "report.json", root / "cache", "normal")
            with self.assertRaises(TranslationCancelled):
                run_translation(*args, old, "cli-default", batch_size=2, concurrency=1)
            self.assertEqual(4, len(old.calls))  # context, 2 initial, 1 polish
            cached = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (root / "cache").rglob("*") if p.is_file()}
            audit(root / "calls" / "old.json", "codex-cli 0.153.4", 1_000_000_000)
            # A failed resume may already have successful calls from the new CLI.
            audit(root / "calls" / "new.json", "codex-cli 0.154.0", 2_000_000_000)
            resumed = provider(root / "calls", "codex-cli 0.154.0")
            self.assertEqual(old.cache_identity, resumed.cache_identity)
            self.assertEqual("codex-cli 0.154.0", resumed.execution_summary()["cli_version"])
            replies = FakeCodex()
            replies.calls = []
            resumed.chat = replies.chat
            run_translation(*args, resumed, "cli-default", batch_size=2, concurrency=1)
            self.assertEqual(1, len(replies.calls))  # only the unfinished polish batch
            self.assertEqual(4, len(parse_srt(root / "result.srt")))
            for path, digest in cached.items():
                self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(1, len(list((root / "cache" / "v1").iterdir())))

    def test_new_job_identity_survives_later_cli_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = provider(root, "codex-cli 0.154.0")
            second = provider(root, "codex-cli 0.155.0")
            self.assertEqual(first.cache_identity, second.cache_identity)
            self.assertEqual(f"{ADAPTER_VERSION}:chatgpt:cli-default", second.cache_identity)
            self.assertEqual("codex-cli 0.155.0", second.execution_summary()["cli_version"])

    def test_malformed_identity_stops_without_replacing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / f".cache-identity-{ADAPTER_VERSION}"
            marker.write_text("incomplete", encoding="utf-8")
            with self.assertRaises(ValueError):
                provider(root, "codex-cli 0.154.0")
            self.assertEqual("incomplete", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
