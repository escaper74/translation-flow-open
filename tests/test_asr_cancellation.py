"""Real sleeping process trees plus synthetic chunk recovery; never real ASR."""
from dataclasses import replace
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from translation_flow.asr import AsrPipeline
from translation_flow.cancellation import run_asr_process
from translation_flow.translation.codex_provider import TranslationCancelled
from scripts import transcribe_whisper_chunked as chunks
from tests import test_asr_packaging as asr_fixtures


def is_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class AsrCancellationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows owned process-tree termination")
    def test_stop_during_a_exits_wrapper_and_whisper_child_without_starting_c(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tools = asr_fixtures.AsrPipelineTests()._tools(root)
            tools = replace(tools, python=Path(sys.executable), transcribe_a=(
                Path(__file__).parent / "fixtures/asr_process_tree.py").resolve())
            source = root / "source.mkv"
            source.write_bytes(b"synthetic media identity only")
            work = root / "chunks"
            stages = []
            requested_at = []
            started = time.monotonic()

            def cancelled():
                if (work / "child.pid").exists() or time.monotonic() - started > 8:
                    if not requested_at:
                        requested_at.append(time.monotonic())
                    return True
                return False

            def events(event, **payload):
                if event == "stage_started":
                    stages.append(payload["stage"])

            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                with patch("translation_flow.asr.check_media_input", return_value={
                    "duration_seconds": 600, "warnings": [],
                }), patch("translation_flow.asr._ascii_a_work_dir", return_value=work):
                    with self.assertRaises(TranslationCancelled):
                        AsrPipeline(tools).run(source, root / "asr", emit=events,
                                               cancelled=cancelled)
                elapsed = time.monotonic() - requested_at[0]
                self.assertLess(elapsed, 5, "stop should not wait for the 30-second stand-in")
                for name in ("parent.pid", "child.pid"):
                    self.assertFalse(is_alive(int((work / name).read_text())))
                self.assertIsNone(unrelated.poll(), "unrelated Python must remain running")
                self.assertEqual(["media_input", "asr_a"], stages)
                self.assertFalse((root / "asr/asr.report.json").exists())
                self.assertEqual(b"synthetic media identity only", source.read_bytes())
                print(f"ASR stop: owned wrapper + child exited in {elapsed:.3f}s; unrelated process retained")
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_output_is_drained_and_exit_status_is_preserved(self):
        result = run_asr_process(
            [sys.executable, "-c", "import sys; print('x'*200000); print('err', file=sys.stderr); sys.exit(7)"],
            cancelled=lambda: False,
        )
        self.assertEqual(7, result.returncode)
        self.assertEqual(200000, len(result.stdout.strip()))
        self.assertEqual("err", result.stderr.strip())

    def test_stop_before_start_does_not_launch_a_process(self):
        with patch("translation_flow.cancellation.subprocess.Popen") as start:
            with self.assertRaises(TranslationCancelled):
                run_asr_process(["must-not-start"], cancelled=lambda: True)
            start.assert_not_called()

    def test_interrupted_nonempty_chunk_is_recomputed_and_completed_chunk_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work = root / "work"
            work.mkdir()
            model = root / "model.bin"
            model.write_bytes(b"fake model")
            complete = work / "chunk_0001_000000s"
            partial = work / "chunk_0002_000600s"
            cue = "1\n00:00:00,000 --> 00:00:01,000\nfixture\n"
            for prefix in (complete, partial):
                prefix.with_suffix(".srt").write_text(cue, encoding="utf-8")
                prefix.with_suffix(".txt").write_text("fixture", encoding="utf-8")
            partial.with_suffix(".incomplete").write_text("interrupted", encoding="utf-8")
            original = complete.with_suffix(".srt").read_bytes()
            calls = []

            def command(args):
                calls.append(args[0])
                if args[0] == "fake-ffmpeg":
                    Path(args[-1]).write_bytes(b"fake audio")
                else:
                    prefix = Path(args[args.index("-of") + 1])
                    prefix.with_suffix(".srt").write_text(cue, encoding="utf-8")
                    prefix.with_suffix(".txt").write_text("complete", encoding="utf-8")

            argv = ["transcribe", "--input-video", str(root / "source"),
                    "--output-srt", str(root / "A.srt"), "--raw-output-srt", str(root / "A.raw.srt"),
                    "--output-txt", str(root / "A.txt"), "--work-dir", str(work),
                    "--ffmpeg", "fake-ffmpeg", "--ffprobe", "fake-ffprobe", "--whisper", "fake-whisper",
                    "--model", str(model), "--report", str(root / "timing.json")]
            with patch.object(sys, "argv", argv), patch.object(chunks, "ffprobe_duration", return_value=1200):
                with patch.object(chunks, "run_command", side_effect=RuntimeError("interrupted")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        chunks.main()
                self.assertTrue(partial.with_suffix(".incomplete").exists())
                self.assertFalse((root / "A.srt").exists())
                # Simulate a killed Whisper having already written both outputs.
                partial.with_suffix(".srt").write_text(cue, encoding="utf-8")
                partial.with_suffix(".txt").write_text("partial", encoding="utf-8")
                with patch.object(chunks, "run_command", side_effect=command):
                    self.assertEqual(0, chunks.main())
                self.assertEqual(["fake-ffmpeg", "fake-whisper"], calls)
                self.assertFalse(partial.with_suffix(".incomplete").exists())
                self.assertEqual(original, complete.with_suffix(".srt").read_bytes())
                timing = json.loads((root / "timing.json").read_text(encoding="utf-8"))
                self.assertEqual(1, timing["reused_chunks"])
                self.assertEqual(0, timing["chunks"][0]["transcribe_seconds"])
                self.assertGreaterEqual(timing["chunks"][1]["transcribe_seconds"], 0)
                calls.clear()
                with patch.object(chunks, "run_command", side_effect=command):
                    self.assertEqual(0, chunks.main())
                self.assertEqual([], calls)
                timing = json.loads((root / "timing.json").read_text(encoding="utf-8"))
                self.assertEqual(2, timing["reused_chunks"])
                self.assertEqual(0, timing["transcribe_seconds"])
