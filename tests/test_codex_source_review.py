from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from translation_flow.srt import SubtitleCue, parse_srt, validate_timeline, write_srt
from translation_flow.translation.provider import FatalProviderError
from translation_flow.translation.source_review import (
    ProviderAttemptsExhausted, prepare_codex_source,
)


class ReviewProvider:
    provider_id = "codex-test"
    cache_identity = "codex-test:chatgpt"

    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def chat(self, messages, *, model, temperature, json_output=False):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload["items"])
        assert temperature == 0 and json_output
        if self.response:
            return self.response(payload["items"], len(self.calls))
        return json.dumps({"items": [
            {"id": item["id"], "text": item["a"], "note": "文本有歧义，待核听。"}
            for item in payload["items"]
        ]}, ensure_ascii=False)


def write_asr(root, a_texts, c_texts=None, accepted_ids=None):
    c_texts = c_texts if c_texts is not None else a_texts
    accepted_ids = accepted_ids if accepted_ids is not None else [
        index for index, (a, c) in enumerate(zip(a_texts, c_texts), 1) if a != c
    ]
    asr = root / "asr"

    def write(name, texts):
        return write_srt(asr / name, [
            SubtitleCue(index, (index - 1) * 1000, index * 1000 - 100, text)
            for index, text in enumerate(texts, 1)
        ])

    a_path = write("A/A.ja.srt", a_texts)
    c_path = write("C/C.ja.srt", c_texts)
    fused = write("fusion/fusion.ja.srt", [
        c if index in accepted_ids else a
        for index, (a, c) in enumerate(zip(a_texts, c_texts), 1)
    ])
    report = {
        "status": "complete", "fusion_srt": str(fused),
        "report_path": str(asr / "asr.report.json"),
        "accepted_matches": [{"a_index": index, "c_index": index} for index in accepted_ids],
    }
    Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
    return fused, report, (a_path, c_path, fused)


class CodexSourceReviewTests(unittest.TestCase):
    def test_unsupported_short_c_collapse_falls_back_without_model_or_source_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = [f"これは異なる文です{i}" for i in range(100)]
            c = ["はい。"] * 75 + a[75:]
            fused, report, originals = write_asr(root, a, c)
            previous = {path: path.read_bytes() for path in originals}
            provider, events = ReviewProvider(), []
            output, result = prepare_codex_source(fused, report, root / "review", provider, "model", events.append)
            self.assertEqual([], provider.calls)
            self.assertEqual(a, [cue.text for cue in parse_srt(output)])
            self.assertEqual(previous, {path: path.read_bytes() for path in originals})
            self.assertEqual("A", result["source_basis"])
            self.assertEqual(75, result["accepted_matches_count"])
            self.assertEqual(75, result["changed_from_fusion_count"])
            self.assertEqual(0, result["c_short_repetition"]["a_overlap_support_count"])
            self.assertTrue(any(event.get("code") == "C_SHORT_REPETITION_FALLBACK_A" for event in events))
            self.assertEqual("pending", result["human_review"])
            self.assertFalse(result["audio_verified"])

    def test_frequent_replies_supported_by_longer_a_are_not_degraded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = ["はい、わかりました。"] * 80 + ["確認をお願いします。"] * 20
            c = ["はい。"] * 80 + a[80:]
            fused, report, _ = write_asr(root, a, c)
            provider = ReviewProvider()
            output, result = prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertFalse(result["c_short_repetition"]["degraded"])
            self.assertEqual(1.0, result["c_short_repetition"]["a_overlap_support_ratio"])
            self.assertEqual([40, 40], list(map(len, provider.calls)))
            self.assertEqual(80, result["uncertain_count"])
            self.assertNotIn("待核听", output.read_text(encoding="utf-8"))

    def test_tiny_or_non_dominant_samples_do_not_trigger_collapse(self):
        for size, reply_count in [(10, 10), (100, 59)]:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                a = [f"別の文章が続きます{i}" for i in range(size)]
                c = ["はい"] * reply_count + a[reply_count:]
                fused, report, _ = write_asr(root, a, c)
                _, result = prepare_codex_source(fused, report, root / "review", ReviewProvider(), "model")
                self.assertFalse(result["c_short_repetition"]["degraded"])

    def test_all_accepted_items_audited_but_only_substantive_differences_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = ["はい。", "そうですね。", "人差し指です。", "ここは病院です。", "次は？"]
            c = ["はい。", "そうですね", "小さじ指です。", "ここは病院です。", "はい。"]
            fused, report, originals = write_asr(root, a, c, [1, 2, 3, 5])
            provider = ReviewProvider()
            output, result = prepare_codex_source(fused, Path(report["report_path"]), root / "review", provider, "model")
            self.assertEqual([3, 5], [item["id"] for item in provider.calls[0]])
            self.assertEqual({"exact_same": 1, "format_only": 1, "text_changed": 2}, result["classification_counts"])
            self.assertEqual(4, len(result["items"]))
            self.assertEqual("そうですね", parse_srt(output)[1].text)
            self.assertEqual("次は？", parse_srt(output)[4].text)
            validate_timeline(parse_srt(originals[0]), parse_srt(output))
            self.assertIn("reference_neighbors", provider.calls[0][0])

    def test_successful_batch_survives_fatal_failure_and_is_not_called_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = [f"こちらが元の文章です{i}" for i in range(41)]
            c = [f"こちらは別の文章です{i}" for i in range(41)]
            fused, report, _ = write_asr(root, a, c)

            def fail_second(items, call):
                if call == 2:
                    raise FatalProviderError("login expired")
                return json.dumps({"items": [{"id": item["id"], "text": item["a"], "note": ""} for item in items]})

            first = ReviewProvider(fail_second)
            with self.assertRaisesRegex(FatalProviderError, "login expired"):
                prepare_codex_source(fused, report, root / "review", first, "model")
            self.assertEqual(2, len(first.calls))
            self.assertEqual(1, len(list((root / "review").glob("*/batch-*.json"))))
            self.assertEqual([], list((root / "review").glob("*/reviewed.ja.srt")))
            resumed = ReviewProvider()
            output, result = prepare_codex_source(fused, report, root / "review", resumed, "model")
            self.assertEqual([[41]], [[item["id"] for item in batch] for batch in resumed.calls])
            self.assertEqual("complete", result["status"])
            completed = ReviewProvider()
            second_output, _ = prepare_codex_source(fused, report, root / "review", completed, "model")
            self.assertEqual(output, second_output)
            self.assertEqual([], completed.calls)

    def test_provider_model_source_and_prompt_cache_identities_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fused, report, _ = write_asr(root, ["元の文章です。"], ["別の文章です。"])
            provider = ReviewProvider()
            destinations = []
            for model, identity in [("m1", "p1"), ("m2", "p1"), ("m1", "p2")]:
                provider.cache_identity = identity
                destinations.append(prepare_codex_source(fused, report, root / "review", provider, model)[0])
            with mock.patch("translation_flow.translation.source_review.PROMPT_VERSION", "changed-prompt"):
                destinations.append(prepare_codex_source(fused, report, root / "review", provider, "m1")[0])
            write_asr(root, ["修正した元の文章です。"], ["別の文章です。"])
            destinations.append(prepare_codex_source(fused, report, root / "review", provider, "m1")[0])
            self.assertEqual(5, len(set(destinations)))
            self.assertEqual(5, len(provider.calls))

    def test_invalid_model_id_or_empty_text_has_two_attempts_and_no_success_cache(self):
        for response in [
            {"items": [{"id": 99, "text": "文章です", "note": ""}]},
            {"items": [{"id": 1, "text": "", "note": ""}]},
        ]:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fused, report, _ = write_asr(root, ["元の文章です。"], ["別の文章です。"])
                provider = ReviewProvider(lambda items, call: json.dumps(response))
                with self.assertRaises(ProviderAttemptsExhausted):
                    prepare_codex_source(fused, report, root / "review", provider, "model")
                self.assertEqual(2, len(provider.calls))
                self.assertEqual([], list((root / "review").glob("*/batch-*.json")))

    def test_cache_write_failure_does_not_repeat_the_successful_model_call(self):
        from translation_flow.translation import source_review

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fused, report, _ = write_asr(root, ["元の文章です。"], ["別の文章です。"])
            original_write = source_review._json_write

            def failing_write(path, value):
                if path.name.startswith("batch-"):
                    raise OSError("disk full")
                original_write(path, value)

            provider = ReviewProvider()
            with mock.patch.object(source_review, "_json_write", side_effect=failing_write):
                with self.assertRaisesRegex(OSError, "disk full"):
                    prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertEqual(1, len(provider.calls))

    def test_codex_metadata_is_retained_and_cancellation_checks_cached_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fused, report, _ = write_asr(root, ["元の文章です。"], ["別の文章です。"])
            metadata = {"authentication": "chatgpt", "resolved_model": None, "elapsed_seconds": 1.2}
            provider = ReviewProvider(lambda items, call: json.dumps({
                "items": [{"id": 1, "text": items[0]["a"], "note": ""}],
                "_codex": metadata,
            }))
            output, result = prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertEqual(metadata, result["batches"][0]["call_metadata"])
            _, cached_result = prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertTrue(cached_result["batches"][0]["resumed"])
            self.assertEqual(metadata, cached_result["batches"][0]["call_metadata"])
            before = output.read_bytes()
            provider.check_cancelled = mock.Mock(side_effect=FatalProviderError("cancelled"))
            with self.assertRaisesRegex(FatalProviderError, "cancelled"):
                prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertEqual(before, output.read_bytes())
            self.assertEqual(1, len(provider.calls))

    def test_original_timeline_or_fusion_provenance_conflict_stops_before_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fused, report, _ = write_asr(root, ["元の文章です。"], ["別の文章です。"])
            write_srt(fused, [SubtitleCue(1, 1, 901, "別の文章です。")])
            provider = ReviewProvider()
            with self.assertRaisesRegex(ValueError, "timecode mismatch"):
                prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertEqual([], provider.calls)
            write_srt(fused, [SubtitleCue(1, 0, 900, "手で変えた文章です。")])
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                prepare_codex_source(fused, report, root / "review", provider, "model")
            self.assertEqual([], provider.calls)


if __name__ == "__main__":
    unittest.main()
