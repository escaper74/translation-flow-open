import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from translation_flow import runtime
from translation_flow.diagnostics import inspect_environment, main


class BackendRuntimeTests(unittest.TestCase):
    def test_explicit_then_environment_then_project_local_paths(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            self.assertEqual(root / "workflow/tools", runtime.tool_root(project_root=root))
            self.assertEqual(runtime.PROJECT_ROOT / "workflow/tools", runtime.tool_root())
            with patch.dict(os.environ, {"TRANSLATION_FLOW_TOOL_ROOT": str(root / "configured"),
                                         "TRANSLATION_FLOW_JASNA": str(root / "configured.exe"),
                                         "TRANSLATION_FLOW_ACCEPTANCE_REPORT": str(root / "configured.json")}):
                self.assertEqual(root / "configured", runtime.tool_root(project_root=root))
                self.assertEqual(root / "explicit", runtime.tool_root(root / "explicit"))
                self.assertEqual(root / "configured.exe", runtime.jasna_path(tools=root))
                self.assertEqual(root / "explicit.exe", runtime.jasna_path(root / "explicit.exe", tools=root))
                self.assertEqual(root / "configured.json", runtime.acceptance_report(tools=root))
                self.assertEqual(root / "explicit.json", runtime.acceptance_report(root / "explicit.json", tools=root))

    def test_srt_api_inventory_needs_no_media_tools_or_cli(self):
        with patch("subprocess.run", side_effect=AssertionError("offline only")), patch.dict(
            os.environ, {"SUBTITLE_API_KEY": "not-for-reports"}
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, main(["--mode", "srt", "--provider", "qwen"]))
            result = json.loads(output.getvalue())
            self.assertEqual(["python"], [item["name"] for item in result["checks"]])
            self.assertNotIn("not-for-reports", output.getvalue())

    def test_missing_video_tools_and_codex_are_named_without_starting_them(self):
        with tempfile.TemporaryDirectory() as temp, patch("translation_flow.diagnostics.resolve_codex_executable", return_value=None), patch(
            "subprocess.run", side_effect=AssertionError("do not run tools")
        ):
            report = inspect_environment(mode="full", provider="codex", tools=Path(temp))
            self.assertEqual("missing_paths", report["status"])
            missing = {item["name"] for item in report["checks"] if not item["present"]}
            self.assertTrue({"codex_cli", "whisper", "ffmpeg", "jasna", "acceptance_report"} <= missing)

    def test_existing_codex_path_is_only_path_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "codex.exe"
            executable.write_bytes(b"path-fixture")
            with patch("shutil.which", return_value=str(executable)):
                report = inspect_environment(mode="srt", provider="codex")
            self.assertEqual("paths_available", report["status"])
            self.assertIn("未验证登录", report["scope"])

    def test_turbo_default_optional_vad_and_explicit_legacy_model(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "subprocess.run", side_effect=AssertionError("inventory must not execute tools")
        ):
            root = Path(temp)
            report = inspect_environment(mode="video", provider="deepseek", tools=root)
            model = next(x for x in report["checks"] if x["name"] == "whisper_model")
            self.assertEqual("ggml-large-v3-turbo.bin", Path(model["path"]).name)
            for item in report["checks"]:
                path = Path(item["path"])
                if item["present"]:
                    continue
                if item["name"] == "faster_model":
                    path.mkdir(parents=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
            report = inspect_environment(mode="video", provider="deepseek", tools=root)
            self.assertEqual("paths_available", report["status"])
            self.assertTrue(report["warnings"])
            self.assertEqual({"vad_cli", "vad_model"}, {x["name"] for x in report["optional_checks"]})
            for item in report["optional_checks"]:
                path = Path(item["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual([], inspect_environment(mode="video", provider="deepseek", tools=root)["warnings"])
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(1, main(["--provider", "deepseek", "--tool-root", str(root),
                                          "--asr-a-model", "large-v3"]))
                legacy = json.loads(output.getvalue())
            self.assertEqual(["whisper_model"], [x["name"] for x in legacy["checks"] if not x["present"]])
            self.assertEqual("large-v3", legacy["asr_a_model"])
