import json
from pathlib import Path
import tempfile
import unittest

from translation_flow.opening_timing import (
    copy_timing, create_candidate, propose_opening, speech_from_log,
)
from translation_flow.srt import SubtitleCue as Cue, parse_srt, write_srt


class OpeningTimingTests(unittest.TestCase):
    def setUp(self):
        self.a = [Cue(1, 0, 2000, "おはようございます"), Cue(2, 2000, 4000, "今日は晴れています"),
                  Cue(3, 22000, 25000, "散歩しましょう")]
        self.c = [Cue(1, 12000, 13700, self.a[0].text), Cue(2, 14100, 15900, self.a[1].text),
                  Cue(3, 22000, 25000, self.a[2].text)]
        self.speech = [(11800, 14000), (14000, 16100), (21800, 25100)]

    def test_only_opening_times_change(self):
        output, report = propose_opening(self.a, self.c, self.speech)
        self.assertEqual(report["status"], "candidate_for_human_review")
        self.assertEqual([x.text for x in output], [x.text for x in self.a])
        self.assertEqual([x.index for x in output], [1, 2, 3])
        self.assertEqual(output[0].start_ms, 12000)
        self.assertEqual(output[1].end_ms, 15900)
        self.assertEqual(output[2], self.a[2])
        self.assertEqual(report["human_review_status"], "pending")

    def test_normal_opening_is_unchanged(self):
        output, report = propose_opening(self.c, self.c, self.speech)
        self.assertEqual(output, self.c)
        self.assertEqual(report["status"], "no_candidate")

    def test_no_speech_keeps_original(self):
        output, _ = propose_opening(self.a, self.c, [])
        self.assertEqual(output, self.a)

    def test_ambiguous_repeated_text_is_not_guessed(self):
        extra = Cue(4, 16000, 18000, self.a[0].text)
        output, report = propose_opening(self.a, self.c + [extra], self.speech)
        self.assertEqual(output, self.a)
        self.assertEqual(report["reason"], "ambiguous_or_weak_c_match")

    def test_reversed_matches_keep_original(self):
        c = [Cue(1, 12000, 14000, self.a[1].text), Cue(2, 14000, 16000, self.a[0].text)]
        self.assertEqual(propose_opening(self.a, c, self.speech)[0], self.a)

    def test_inconsistent_offsets_keep_original(self):
        c = [self.c[0], Cue(2, 18000, 20000, self.a[1].text)]
        output, report = propose_opening(self.a, c, self.speech + [(18000, 20000)])
        self.assertEqual(output, self.a)
        self.assertEqual(report["reason"], "opening_shifts_disagree")

    def test_c_intervals_need_speech_support(self):
        self.assertEqual(propose_opening(self.a, self.c, [(10000, 11000)])[0], self.a)

    def test_candidate_must_not_reach_untouched_cue(self):
        c = [self.c[0], Cue(2, 14100, 23000, self.a[1].text)]
        self.assertEqual(propose_opening(self.a, c, self.speech)[0], self.a)

    def test_same_timing_is_applied_without_changing_chinese(self):
        corrected, _ = propose_opening(self.a, self.c, self.speech)
        zh = [Cue(x.index, x.start_ms, x.end_ms, "中文" + str(x.index)) for x in self.a]
        out = copy_timing(self.a, corrected, zh)
        self.assertEqual([x.text for x in out], [x.text for x in zh])
        self.assertEqual(out[2], zh[2])
        with self.assertRaises(ValueError):
            copy_timing(self.a, corrected, zh[1:])

    def test_whisper_vad_centiseconds_and_completion(self):
        text = "Detected 2 speech segments:\nSpeech segment 0: start = 1345.00, end = 1628.00\nSpeech segment 1: start = 1687.00, end = 1907.00"
        self.assertEqual(speech_from_log(text, 60000), [(13450, 16280), (16870, 19070)])
        for bad in ["", text.replace("Detected 2", "Detected 3"), text.replace("1907.00", "19070.00")]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                speech_from_log(bad, 60000)

    def test_outputs_are_separate_and_originals_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / name for name in ["a.srt", "c.srt", "ja.srt", "zh.srt"]]
            for path, cues in zip(paths, [self.a, self.c, self.a, self.a]):
                write_srt(path, cues)
            before = [p.read_bytes() for p in paths]
            report = create_candidate(*paths, self.speech, root / "candidate")
            self.assertTrue(report["inputs_unchanged"])
            self.assertTrue(report["all_text_preserved"])
            self.assertEqual(report["remaining_cues_unchanged"], 1)
            self.assertEqual([p.read_bytes() for p in paths], before)
            self.assertEqual(parse_srt(report["outputs"]["chinese"])[2], self.a[2])
            saved = json.loads((root / "candidate/timing-candidate.report.json").read_text())
            self.assertFalse(saved["production_enabled"])
            with self.assertRaises(FileExistsError):
                create_candidate(*paths, self.speech, root / "candidate")
            self.assertEqual([p.read_bytes() for p in paths], before)


if __name__ == "__main__":
    unittest.main()
