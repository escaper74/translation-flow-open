"""Exercise the real ASR handoff; media/model calls are isolated fixtures."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from tests import test_asr_packaging as fixtures
from translation_flow.asr import AsrPipeline, conservative_fuse, load_completed_asr
from translation_flow.opening_timing import OPENING_TIMING_POLICY
from translation_flow.srt import SubtitleCue as Cue, parse_srt, validate_timeline, write_srt
from translation_flow.translation.codex_provider import TranslationCancelled


class OpeningHandoffTests(unittest.TestCase):
    def _run(self, root, *, legacy=False, broken_vad=False, cancel_vad=False, missing_vad=False):
        tools = fixtures.AsrPipelineTests()._tools(root)
        cli, model = root / 'vad.exe', root / 'vad.bin'
        if not missing_vad:
            cli.write_bytes(b'test executable')
            model.write_bytes(b'test model')
        tools = replace(tools, vad_cli=cli, vad_model=model)
        source = root / 'source.mp4'
        source.write_bytes(b'media')
        job = root / 'asr'
        if legacy:
            job.mkdir()
            (job / 'asr.input.json').write_text(json.dumps({
                'source': str(source), 'source_size_bytes': source.stat().st_size,
                'source_mtime_ns': source.stat().st_mtime_ns, 'asr_audio_selection': 'first',
                'a_model': 'large-v3',
            }))
        a = [Cue(1, 0, 2000, 'おはようございます'), Cue(2, 2000, 4000, '今日は晴れています'),
             Cue(3, 22000, 25000, '散歩しましょう')]
        c = [Cue(1, 12000, 13700, a[0].text), Cue(2, 14100, 15900, a[1].text),
             Cue(3, 22100, 24900, a[2].text + '。')]
        calls, events = [], []

        def run(args, **kwargs):
            calls.append(args)
            if len(args) > 1 and args[1] == str(tools.transcribe_a):
                write_srt(args[args.index('--raw-output-srt') + 1], a)
                write_srt(args[args.index('--output-srt') + 1], a)
                work = Path(args[args.index('--work-dir') + 1])
                work.mkdir(parents=True, exist_ok=True)
                with wave.open(str(work / 'chunk_0001_000000s.wav'), 'wb') as wav:
                    wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    wav.writeframes(bytes(30 * 16000 * 2))
            elif len(args) > 1 and args[1] == str(tools.transcribe_c):
                write_srt(args[args.index('--output-srt') + 1], c)
            elif len(args) > 1 and args[1] == str(tools.clean_srt):
                write_srt(args[args.index('--output') + 1], parse_srt(args[args.index('--input') + 1]))
            elif len(args) > 1 and args[1] in (str(tools.validate_quality), str(tools.validate_structure)):
                Path(args[args.index('--report') + 1]).write_text('{"failures": [], "warnings": []}')
            elif args[0] == str(cli):
                if cancel_vad:
                    raise TranslationCancelled('test stop')
                if broken_vad:
                    return subprocess.CompletedProcess(args, 2, '', 'test detector failure')
                return subprocess.CompletedProcess(args, 0,
                    'Detected 3 speech segments:\n'
                    'Speech segment 0: start = 1180.00, end = 1400.00\n'
                    'Speech segment 1: start = 1400.00, end = 1610.00\n'
                    'Speech segment 2: start = 2180.00, end = 2510.00', '')
            else:
                raise AssertionError(f'Unexpected test process: {args[0]}')
            return subprocess.CompletedProcess(args, 0, '', '')

        pipeline = AsrPipeline(tools, process_runner=run)
        with patch('translation_flow.asr.check_media_input', return_value={
            'warnings': [], 'duration_seconds': 30,
        }):
            fused, report = pipeline.run(source, job, lambda event, **kw: events.append((event, kw)))
        return tools, source, job, a, c, fused, report, calls, events

    def test_new_job_corrects_before_translation_and_preserves_original_text(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, source, job, a, c, fused, report, calls, events = self._run(Path(directory))
            self.assertEqual(report['opening_timing']['status'], 'applied')
            self.assertEqual(parse_srt(job / 'A/A.recognized.ja.srt'), a)
            effective = parse_srt(job / 'A/A.ja.srt')
            self.assertEqual(effective[0].start_ms, 12000)
            self.assertEqual(effective[2], a[2])
            uncorrected_fusion, matches = conservative_fuse(a, c)
            actual_fusion = parse_srt(fused)
            self.assertEqual([x.text for x in actual_fusion], [x.text for x in uncorrected_fusion])
            validate_timeline(effective, actual_fusion)
            self.assertEqual(actual_fusion[2], uncorrected_fusion[2])
            positions = {name: next(i for i, cmd in enumerate(calls) if name in cmd)
                         for name in (str(tools.transcribe_c), str(tools.vad_cli))}
            self.assertLess(positions[str(tools.transcribe_c)], positions[str(tools.vad_cli)])
            self.assertFalse(any('--use-gpu' in cmd for cmd in calls))
            marker = json.loads((job / 'asr.input.json').read_text())
            self.assertEqual(marker['opening_timing_policy'], OPENING_TIMING_POLICY)
            # Same loader used by Codex/API/full-flow must accept the corrected backbone.
            paths = list(job.rglob('*'))
            before = {str(p): p.read_bytes() for p in paths if p.is_file()}
            resumed, saved = load_completed_asr(source, job)
            self.assertEqual(resumed, fused)
            self.assertEqual(saved, report)
            self.assertEqual(before, {str(p): p.read_bytes() for p in paths if p.is_file()})

    def test_legacy_partial_job_does_not_acquire_new_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, _, job, a, _, fused, report, calls, _ = self._run(Path(directory), legacy=True)
            self.assertEqual(report['opening_timing']['status'], 'legacy_unchanged')
            self.assertEqual(parse_srt(job / 'A/A.ja.srt'), a)
            self.assertFalse((job / 'A/A.recognized.ja.srt').exists())
            self.assertFalse(any(cmd[0] == str(tools.vad_cli) for cmd in calls))
            validate_timeline(a, parse_srt(fused))

    def test_detector_failure_and_missing_model_are_nonblocking(self):
        for options in ({'broken_vad': True}, {'missing_vad': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                _, _, job, a, _, fused, report, _, events = self._run(Path(directory), **options)
                self.assertEqual(report['status'], 'complete')
                self.assertEqual(report['opening_timing']['status'], 'skipped')
                self.assertEqual(parse_srt(job / 'A/A.ja.srt'), a)
                validate_timeline(a, parse_srt(fused))
                self.assertTrue(any(p.get('code') == 'OPENING_TIMING_SKIPPED' for _, p in events))

    def test_user_cancellation_is_not_downgraded_to_optional_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TranslationCancelled):
                self._run(Path(directory), cancel_vad=True)
            self.assertFalse((Path(directory) / 'asr/asr.report.json').exists())


if __name__ == '__main__':
    unittest.main()
