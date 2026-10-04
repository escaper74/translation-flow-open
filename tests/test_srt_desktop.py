from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from translation_flow import srt_desktop
from translation_flow.translation.api_providers import (
    API_PROVIDERS, DEFAULT_ENDPOINTS, api_cache_identity, resolve_endpoint,
)
from urllib.parse import urlsplit
from test_srt_codex_cli import FakeCodex, FIXTURE


class SrtDesktopTests(unittest.TestCase):
    def request(self, root, provider="codex"):
        source = root / "会议.srt"
        source.write_bytes(FIXTURE.read_bytes())
        job = root / ".srt-translation" / "srt-test"
        job.mkdir(parents=True)
        config = {"id": provider, "host": "chatgpt.com", "model": "cli-default"}
        if provider != "codex":
            config.update(host=urlsplit(resolve_endpoint(provider).url).netloc,
                          model="deepseek-v4-pro" if provider == "deepseek" else "explicit-model")
            if provider != "deepseek":
                config.update(endpoint_profile=DEFAULT_ENDPOINTS[provider], max_output_tokens=8192)
        value = {"schema_version": 1, "workflow_mode": "srt_translation", "job_id": job.name,
                 "input_srt": str(source), "output_dir": str(root), "profile_id": "normal",
                 "provider": config, "consent": {"provider": provider, "current_run": True,
                 "subtitle_text_only": True, "audio_video_local": True}}
        path = job / "request.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def call(self, path, provider=None, *, key=True):
        config = json.loads(path.read_text())["provider"]
        provider = provider or FakeCodex()
        provider.provider_id = config["id"]
        if provider.provider_id != "codex":
            provider.cache_identity = api_cache_identity(provider.provider_id,
                config.get("endpoint_profile"), config.get("max_output_tokens", 8192))
        output = io.StringIO()
        with patch.dict(os.environ, {"SUBTITLE_API_KEY": "fake-unused-key"} if key else {}, clear=True), \
             patch("translation_flow.translation.cli.CodexProvider", return_value=provider), \
             patch("translation_flow.translation.cli.create_api_provider", return_value=provider), \
             patch("sys.stdout", output), patch("sys.stderr", new_callable=io.StringIO):
            status = srt_desktop.main(["--request", str(path)])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn("fake-unused-key", output.getvalue())
        return status, events, provider

    def test_seven_providers_publish_srt_events_and_resume_without_provider(self):
        for name in (*API_PROVIDERS, "codex"):
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as raw:
                path, request = self.request(Path(raw), name)
                source = Path(request["input_srt"])
                before = source.read_bytes()
                status, events, provider = self.call(path)
                self.assertEqual(status, 0, events)
                self.assertEqual(events[-1]["event"], "job_completed")
                self.assertEqual(events[-1]["technical_validation"]["status"], "passed")
                output = Path(events[-1]["outputs"]["chinese_srt"])
                self.assertTrue(output.is_file())
                self.assertEqual(source.read_bytes(), before)
                calls = len(provider.calls)
                with patch("translation_flow.srt_desktop.srt_cli.main") as cli:
                    status, replay, _ = self.call(path, provider, key=False)
                    cli.assert_not_called()
                self.assertEqual(status, 0, replay)
                self.assertTrue(replay[-1]["reused"])
                self.assertGreater(replay[0]["seq"], events[-1]["seq"])
                self.assertEqual(len(provider.calls), calls)
                self.assertNotIn("fake-unused-key", "".join(
                    p.read_text(encoding="utf-8") for p in path.parent.rglob("*.json")))

    def test_invalid_request_rejected_before_cli(self):
        for mutation in (lambda v: v.update(api_key="do-not-log"),
                         lambda v: v["consent"].update(provider="qwen"),
                         lambda v: v["provider"].update(max_output_tokens=8192),
                         lambda v: v.update(workflow_mode="translation_picture_final")):
            with tempfile.TemporaryDirectory() as raw:
                path, value = self.request(Path(raw))
                mutation(value)
                path.write_text(json.dumps(value), encoding="utf-8")
                with patch("translation_flow.srt_desktop.srt_cli.main") as cli, \
                     patch("sys.stderr", new_callable=io.StringIO) as error:
                    self.assertEqual(srt_desktop.main(["--request", str(path)]), 1)
                    cli.assert_not_called()
                    self.assertNotIn("do-not-log", error.getvalue())

    def test_stop_keeps_successful_batches_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as raw:
            path, _ = self.request(Path(raw), "qwen")
            provider = FakeCodex()
            original_chat = provider.chat
            def chat(*args, **kwargs):
                response = original_chat(*args, **kwargs)
                if len(provider.calls) == 2:
                    (path.parent / "cancel.requested").write_text("stop", encoding="utf-8")
                return response
            provider.chat = chat
            status, events, _ = self.call(path, provider)
            self.assertEqual(status, 2, events)
            self.assertTrue(list((path.parent / "cache").rglob("batches/translate/*.json")))
            self.assertFalse((path.parent / "job.result.json").exists())
            self.assertEqual(events[-1]["event"], "job_cancelled")
            provider.chat = original_chat
            before = len(provider.calls)
            status, events, _ = self.call(path, provider)
            self.assertEqual(status, 0, events)
            self.assertFalse((path.parent / "cancel.requested").exists())
            self.assertEqual(len(provider.calls) - before, 1)  # only polish remained

    def test_changed_source_output_or_configuration_blocks_reuse(self):
        for target in ("source", "output", "config"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as raw:
                path, value = self.request(Path(raw))
                status, events, _ = self.call(path)
                self.assertEqual(status, 0)
                if target == "config":
                    value["profile_id"] = "adult_direct"
                    path.write_text(json.dumps(value), encoding="utf-8")
                else:
                    file = Path(value["input_srt"] if target == "source" else events[-1]["outputs"]["chinese_srt"])
                    file.write_text(file.read_text(encoding="utf-8").replace("。", "！").replace("中文译文", "修改内容"), encoding="utf-8")
                with patch("translation_flow.srt_desktop.srt_cli.main") as cli:
                    status, events, _ = self.call(path)
                    self.assertEqual(status, 1, events)
                    cli.assert_not_called()
                self.assertEqual(events[-1]["event"], "job_failed")

    def test_partial_published_output_is_preserved_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, _ = self.request(root)
            output = root / "会议.srt-test.zh-CN.srt"
            output.write_text("existing-output", encoding="utf-8")
            with patch("translation_flow.srt_desktop.srt_cli.main") as cli:
                status, events, _ = self.call(path)
                self.assertEqual(status, 1, events)
                cli.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "existing-output")


if __name__ == "__main__":
    unittest.main()
