"""Offline checks for C repetition handling; no recognition model is loaded."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.asr_fusion import transcribe_faster_whisper
from tests import test_asr_packaging as packaging_fixture
from tests.test_asr_packaging import _completed, _input_preflight
from translation_flow.asr import AsrPipeline, filter_repeated_c, conservative_fuse

from translation_flow.srt import SubtitleCue, parse_srt, write_srt

ROOT = Path(__file__).resolve().parents[1]
CUE = "1\n00:00:00,000 --> 00:00:01,000\nA dialogue\n"


class CRepetitionTests(unittest.TestCase):

    def test_repeated_model_phrase_removes_fragmented_candidates_but_preserves_a(self):
        a = [SubtitleCue(i, i*1000, i*1000+900, f"Actual dialogue {i}") for i in range(1,61)]
        model = [SubtitleCue(i, i*1000, i*1000+900, "Repeated hallucination?") for i in range(1,61)]
        c = [SubtitleCue(i, i*1000, i*1000+800, "hallucination fragment") for i in range(1,61)]
        kept, report = filter_repeated_c(a, c, model)
        self.assertEqual([], kept)
        self.assertEqual(60, report["removed_model_segments"])
        self.assertEqual(60, report["removed_cues"])
        fused, matches = conservative_fuse(a, kept)
        self.assertEqual(a, fused)
        self.assertFalse(any(match.accepted for match in matches))

    def test_a_supported_repetition_and_rare_phrases_are_kept(self):
        c = [SubtitleCue(i, i*1000, i*1000+800, "Repeated but actual dialogue") for i in range(1,61)]
        kept, report = filter_repeated_c(c, c, c)
        self.assertEqual(c, kept)
        self.assertEqual(0, report["removed_cues"])
        a = [SubtitleCue(i, i*1000, i*1000+800, f"Other dialogue {i}") for i in range(1,61)]
        kept, report = filter_repeated_c(a, c[:3], c[:3])
        self.assertEqual(c[:3], kept)
        self.assertEqual(0, report["removed_cues"])

    def test_all_c_removed_still_allows_source_review_without_external_call(self):
        from tests.test_codex_source_review import write_asr, ReviewProvider
        from translation_flow.translation.source_review import prepare_codex_source
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fused, report, (a_path, c_path, _) = write_asr(root, ["Actual dialogue"], accepted_ids=[])
            c_path.write_text("", encoding="utf-8")
            report["c_repetition_filter"] = {"removed_cues": 1, "kept_cues": 0}
            provider = ReviewProvider()
            reviewed, review = prepare_codex_source(
                fused, report, provider=provider, model="fixture", destination_dir=root/"review")
            self.assertEqual(parse_srt(a_path), parse_srt(reviewed))
            self.assertEqual([], provider.calls)

    def test_complete_c_decoding_can_resume_without_another_model_process(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tools = packaging_fixture.AsrPipelineTests()._tools(root)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            job = root / "job"
            c_dir = job / "C"
            c_dir.mkdir(parents=True)
            stat = source.stat()
            (job/"asr.input.json").write_text(json.dumps({
                "source": str(source), "source_size_bytes":stat.st_size,
                "source_mtime_ns":stat.st_mtime_ns, "a_model":"large-v3",
                "asr_audio_selection":"first"}))
            for name in ("C.raw.srt","C.model-segments.srt"):
                (c_dir/name).write_text(CUE)
            (c_dir/"C.words.json").write_text(json.dumps({"segments":[{"id":0}]}))
            (c_dir/"C.transcription.json").write_text(json.dumps({
                "engine":"faster-whisper","model_segments":1,"resegmented_cues":1}))
            original = {p.name:p.read_bytes() for p in c_dir.iterdir()}
            def runner(args, **kwargs):
                preflight = _input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if args[0] == str(tools.faster_python):
                    self.fail("completed C decoding was sent to GPU again")
                if len(args)>1 and args[1] == str(tools.transcribe_a):
                    for flag in ("--raw-output-srt","--output-srt"):
                        Path(args[args.index(flag)+1]).write_text(CUE)
                elif len(args)>1 and args[1] == str(tools.clean_srt):
                    Path(args[args.index("--output")+1]).write_bytes(Path(args[args.index("--input")+1]).read_bytes())
                elif len(args)>1 and args[1] in (str(tools.validate_quality),str(tools.validate_structure)):
                    Path(args[args.index("--report")+1]).write_text('{"failures":[]}')
                return _completed(args)
            fused, report = AsrPipeline(tools, process_runner=runner).run(source,job)
            self.assertTrue(fused.is_file())
            saved = Path(report["preserved_c_attempt"])
            self.assertEqual(original,{p.name:p.read_bytes() for p in saved.iterdir()})

    def test_quality_gate_still_rejects_repetition_and_summary_omits_text(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repeated = "PRIVATE_FIXTURE_REPEATED_PHRASE"
            cases = (
                ([repeated] * 80, 1),
                ([repeated] * 12 + [f"Unique sentence {i}" for i in range(88)], 1),
                ([f"Unique sentence {i}" for i in range(80)], 0),
            )
            for texts, expected in cases:
                source = root / "input.srt"
                report = root / "quality.json"
                source.write_text("\n\n".join(
                    f"{i}\n00:00:00,000 --> 00:00:01,000\n{text}"
                    for i, text in enumerate(texts, 1)), encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/validate_srt_quality.py"),
                     "--input", str(source), "--report", str(report)],
                    capture_output=True, text=True, encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=False)
                self.assertEqual(expected, result.returncode)
                payload = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(bool(expected), bool(payload["failures"]))
                self.assertNotIn(repeated, result.stderr)
                if expected:
                    self.assertIn("识别结果出现大量重复", result.stderr)
                    self.assertEqual(repeated, payload["top_text"])
                else:
                    self.assertFalse(result.stderr)

    def test_c_cli_uses_independent_windows_and_records_actual_decode_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.media"
            source.write_bytes(b"not decoded by the offline engine")
            model_path = root / "model"
            model_path.mkdir()
            captured = {}
            word = SimpleNamespace(start=0.0, end=1.0, word="Hello.", probability=0.9)
            segment = SimpleNamespace(id=0, start=0.0, end=1.0, text="Hello.",
                                      words=[word], avg_logprob=-0.1, no_speech_prob=0.0)
            def transcribe(source, **kwargs):
                captured.update(kwargs)
                info = SimpleNamespace(language="ja", language_probability=1.0,
                                       duration=1.0, duration_after_vad=1.0)
                return iter([segment]), info
            engine = SimpleNamespace(WhisperModel=lambda *a, **kw: SimpleNamespace(transcribe=transcribe))
            argv = ["c", "--input", str(source), "--model", str(model_path)]
            for flag, name in (("--raw-json", "words.json"), ("--model-srt", "model.srt"),
                               ("--output-srt", "out.srt"), ("--output-txt", "out.txt"),
                               ("--report", "report.json")):
                argv.extend([flag, str(root / name)])
            with patch.dict(sys.modules, {"faster_whisper": engine}), patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, transcribe_faster_whisper.main())
            self.assertFalse(captured["condition_on_previous_text"])
            self.assertTrue(captured["vad_filter"])
            self.assertEqual("ja", captured["language"])
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual({k: captured[k] for k in report["decoding"]}, report["decoding"])
            self.assertTrue((root / "out.srt").is_file())

    def test_failed_c_retry_preserves_evidence_and_complete_asr_reuses_result(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tools = packaging_fixture.AsrPipelineTests()._tools(root)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            attempts = 0
            def runner(args, **kwargs):
                nonlocal attempts
                preflight = _input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if len(args) > 1 and args[1] == str(tools.transcribe_a):
                    for flag in ("--raw-output-srt", "--output-srt"):
                        Path(args[args.index(flag) + 1]).write_text(CUE, encoding="utf-8")
                elif args[0] == str(tools.faster_python):
                    attempts += 1
                    cue = CUE.replace("A dialogue", "First C attempt" if attempts == 1 else "A dialogue.")
                    Path(args[args.index("--output-srt") + 1]).write_text(cue, encoding="utf-8")
                elif len(args) > 1 and args[1] == str(tools.clean_srt):
                    Path(args[args.index("--output") + 1]).write_bytes(Path(args[args.index("--input") + 1]).read_bytes())
                elif len(args) > 1 and args[1] in (str(tools.validate_quality), str(tools.validate_structure)):
                    report = Path(args[args.index("--report") + 1])
                    failed = report.parent.name == "C" and args[1] == str(tools.validate_quality) and attempts == 1
                    report.write_text(json.dumps({"failures": ["fixture repetition"] if failed else []}), encoding="utf-8")
                    if failed:
                        return _completed(args, returncode=1)
                return _completed(args)
            pipeline = AsrPipeline(tools, process_runner=runner)
            job = root / "job"
            with self.assertRaisesRegex(RuntimeError, "C quality gate"):
                pipeline.run(source, job)
            original = {p.name: p.read_bytes() for p in (job / "C").iterdir()}
            occupied = job / "C.interrupted-01"
            occupied.mkdir()
            (occupied / "keep").write_bytes(b"earlier evidence")
            result, report = pipeline.run(source, job)
            preserved = job / "C.interrupted-02"
            self.assertEqual(original, {p.name: p.read_bytes() for p in preserved.iterdir()})
            self.assertEqual(b"earlier evidence", (occupied / "keep").read_bytes())
            self.assertEqual(str(preserved), report["preserved_c_attempt"])
            self.assertTrue(result.is_file())
            def unexpected(*args, **kwargs):
                self.fail("completed ASR started another process")
            reused, _ = AsrPipeline(tools, process_runner=unexpected).run(source, job)
            self.assertEqual(result, reused)
            self.assertEqual(2, attempts)
