from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL
from translation_flow.srt import SubtitleCue, parse_srt, validate_timeline, write_srt
from translation_flow.translation import (
    DeepSeekProvider,
    FatalProviderError,
    RetryableProviderError,
    TranslationPipelineError,
    TranslationValidationError,
    get_profile,
    run_translation,
)
from translation_flow.translation.cli import build_parser, main as cli_main


def _json_at_end(text: str) -> dict:
    start = text.rfind('{"items"')
    return json.loads(text[start:])


class FakeProvider:
    provider_id = "fake"
    default_model = "fake-model"

    def __init__(self, identity: str = "fake:one", max_items: int | None = None):
        self.cache_identity = identity
        self.max_items = max_items
        self.calls: list[tuple[str, list[dict[str, str]], bool]] = []

    def check_connection(self) -> None:
        return None

    def chat(
        self, messages, *, model: str, temperature: float, json_output: bool = False
    ) -> str:
        copied = [dict(message) for message in messages]
        self.calls.append((model, copied, json_output))
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if system.startswith("为日语"):
            return "语境-" + str(len(self.calls))
        if system.startswith("合并分段"):
            return "合并语境"
        payload = _json_at_end(user)
        items = payload["items"]
        if self.max_items is not None and len(items) > self.max_items:
            return "{}"
        if system.startswith("对照日文润色"):
            result = [{"id": item["id"], "text": f"润-{item['zh']}"} for item in items]
        elif system.startswith("定点修复"):
            result = [{"id": item["id"], "text": f"修-{item['id']}"} for item in items]
        else:
            result = [{"id": item["id"], "text": f"译-{item['text']}"} for item in items]
        return json.dumps({"items": result}, ensure_ascii=False)


def _write_source(root: Path, texts: list[str], name: str = "source.srt") -> Path:
    cues = [
        SubtitleCue(index + 1, index * 1_000, index * 1_000 + 900, text)
        for index, text in enumerate(texts)
    ]
    return write_srt(root / name, cues)


class SrtContractTests(unittest.TestCase):
    def test_round_trip_and_timeline_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "字幕.srt"
            cues = [SubtitleCue(7, 1_234, 5_678, "第一行\n第二行")]
            self.assertEqual(path, write_srt(path, cues))
            self.assertEqual(cues, parse_srt(path))
            validate_timeline(cues, parse_srt(path))
            with self.assertRaisesRegex(ValueError, "timecode mismatch"):
                validate_timeline(cues, [SubtitleCue(7, 1_235, 5_678, "文本可变")])

    def test_parser_rejects_bad_blocks_duplicate_ids_and_reverse_time(self) -> None:
        cases = [
            "garbage\n",
            "1\n00:00:00.000 --> 00:00:01,000\nx\n",
            "1\n00:00:02,000 --> 00:00:01,000\nx\n",
            "1\n00:00:01,000 --> 00:00:01,000\nx\n",
            "1\n00:00:00,000 --> 00:00:01,000\nx\n\n1\n00:00:01,000 --> 00:00:02,000\ny\n",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.srt"
            for value in cases:
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        parse_srt(path)

    def test_writer_rejects_non_positive_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "zero.srt"
            with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                write_srt(path, [SubtitleCue(1, 500, 500, "声")])
            self.assertFalse(path.exists())


class TranslationPipelineTests(unittest.TestCase):
    def test_zero_duration_source_is_rejected_before_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,500 --> 00:00:00,500\n声\n",
                encoding="utf-8",
            )
            provider = FakeProvider()

            with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                run_translation(
                    source,
                    root / "out.srt",
                    root / "report.json",
                    root / "cache",
                    "normal",
                    provider,
                    "m1",
                    use_global_context=False,
                    polish=False,
                )

            self.assertEqual([], provider.calls)
            self.assertFalse((root / "out.srt").exists())
            self.assertFalse((root / "report.json").exists())

    def test_translation_then_second_pass_polish_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            report = run_translation(
                _write_source(root, ["一", "二"]),
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "normal",
                FakeProvider(),
                "m1",
                progress_callback=events.append,
                use_global_context=False,
                retries=1,
            )
            self.assertEqual(["润-译-一", "润-译-二"], [cue.text for cue in parse_srt(root / "out.srt")])
            self.assertEqual("passed", report["status"])
            self.assertIn("translation_batch_completed", [event["event"] for event in events])
            self.assertIn("polish_batch_completed", [event["event"] for event in events])
            self.assertEqual("translation_completed", events[-1]["event"])

    def test_two_fixed_contiguous_lanes_then_polish_barrier(self) -> None:
        class FixedLaneProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__("fake:fixed-lanes")
                self.lock = threading.Lock()
                self.first_translation_calls = threading.Barrier(2)
                self.translation_batches_by_thread: dict[str, list[list[int]]] = {}
                self.initial_completed = 0
                self.initial_completed_when_polish_started: int | None = None

            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                system = messages[0]["content"]
                if system.startswith("你是日语"):
                    items = _json_at_end(messages[-1]["content"])["items"]
                    ids = [item["id"] for item in items]
                    thread_name = threading.current_thread().name
                    with self.lock:
                        sequence = self.translation_batches_by_thread.setdefault(
                            thread_name, []
                        )
                        first_in_lane = not sequence
                        sequence.append(ids)
                    if first_in_lane:
                        self.first_translation_calls.wait(timeout=5)
                    content = super().chat(
                        messages,
                        model=model,
                        temperature=temperature,
                        json_output=json_output,
                    )
                    with self.lock:
                        self.initial_completed += 1
                    return content
                if system.startswith("对照日文润色"):
                    with self.lock:
                        if self.initial_completed_when_polish_started is None:
                            self.initial_completed_when_polish_started = (
                                self.initial_completed
                            )
                return super().chat(
                    messages,
                    model=model,
                    temperature=temperature,
                    json_output=json_output,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = FixedLaneProvider()
            events: list[dict] = []
            source = _write_source(
                root,
                [f"固定长度台词{index:02d}" for index in range(1, 9)],
            )
            report = run_translation(
                source,
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "adult_direct",
                provider,
                "m1",
                progress_callback=events.append,
                use_global_context=False,
                batch_size=2,
                retries=1,
                concurrency=2,
            )

            lane_sequences = sorted(
                provider.translation_batches_by_thread.values(),
                key=lambda sequence: sequence[0][0],
            )
            self.assertEqual(
                [[[1, 2], [3, 4]], [[5, 6], [7, 8]]],
                lane_sequences,
            )
            self.assertEqual(4, provider.initial_completed_when_polish_started)
            self.assertEqual(list(range(1, 9)), [cue.index for cue in parse_srt(root / "out.srt")])
            self.assertEqual(2, report["concurrency"])
            self.assertEqual(
                "fixed_contiguous_ranges_balanced_by_source_characters",
                report["lane_strategy"],
            )
            initial_starts = [
                event
                for event in events
                if event["event"] == "translation_batch_started"
            ]
            self.assertEqual({1, 2}, {event["lane"] for event in initial_starts[:2]})
            first_completed_position = next(
                index
                for index, event in enumerate(events)
                if event["event"] == "translation_batch_completed"
            )
            self.assertTrue(
                all(
                    events.index(event) < first_completed_position
                    for event in initial_starts[:2]
                )
            )
            initial_events = [
                event
                for event in events
                if event["event"] == "translation_batch_completed"
            ]
            self.assertEqual([1, 2, 3, 4], [event["current"] for event in initial_events])
            self.assertEqual({1, 2}, {event["lane"] for event in initial_events})
            adult_style = get_profile("adult_direct").style_instruction
            self.assertTrue(
                all(
                    adult_style in messages[0]["content"]
                    for _, messages, _ in provider.calls
                    if messages[0]["content"].startswith(
                        ("你是日语", "对照日文润色")
                    )
                )
            )

    def test_concurrency_does_not_change_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_source(root, [f"台词{index}" for index in range(8)])
            first = FakeProvider("fake:same-concurrency-cache")
            run_translation(
                source,
                root / "parallel.srt",
                None,
                root / "cache",
                "adult_direct",
                first,
                "m1",
                use_global_context=False,
                polish=False,
                batch_size=2,
                retries=1,
                concurrency=2,
            )

            resumed = FakeProvider("fake:same-concurrency-cache")
            run_translation(
                source,
                root / "serial.srt",
                None,
                root / "cache",
                "adult_direct",
                resumed,
                "m1",
                use_global_context=False,
                polish=False,
                batch_size=2,
                retries=1,
                concurrency=1,
            )
            self.assertEqual([], resumed.calls)
            self.assertEqual(
                [cue.text for cue in parse_srt(root / "parallel.srt")],
                [cue.text for cue in parse_srt(root / "serial.srt")],
            )

    def test_lane_failure_stops_new_batches_but_keeps_inflight_cache(self) -> None:
        class FailingLaneProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__("fake:lane-failure")
                self.first_calls = threading.Barrier(2)
                self.failure_announced = threading.Event()
                self.started_ids: list[list[int]] = []
                self.lock = threading.Lock()

            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                items = _json_at_end(messages[-1]["content"])["items"]
                ids = [item["id"] for item in items]
                with self.lock:
                    self.started_ids.append(ids)
                self.first_calls.wait(timeout=5)
                if ids == [1, 2]:
                    self.failure_announced.set()
                    raise FatalProviderError("synthetic fatal lane failure")
                self.failure_announced.wait(timeout=5)
                time.sleep(0.05)
                return super().chat(
                    messages,
                    model=model,
                    temperature=temperature,
                    json_output=json_output,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = FailingLaneProvider()
            with self.assertRaisesRegex(FatalProviderError, "synthetic fatal"):
                run_translation(
                    _write_source(root, [f"台词{index}" for index in range(1, 9)]),
                    root / "out.srt",
                    None,
                    root / "cache",
                    "normal",
                    provider,
                    "m1",
                    use_global_context=False,
                    polish=False,
                    batch_size=2,
                    retries=1,
                    concurrency=2,
                )

            self.assertEqual([[1, 2], [5, 6]], sorted(provider.started_ids))
            cached_ids = {
                item["id"]
                for path in (root / "cache").rglob("*.json")
                for item in json.loads(path.read_text(encoding="utf-8"))["items"]
            }
            self.assertEqual({5, 6}, cached_ids)
            self.assertFalse((root / "out.srt").exists())

    def test_long_context_covers_every_cue_and_short_context_uses_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texts = [f"唯一原文-{index}-abcdefghij" for index in range(5)]
            source = _write_source(root, texts)
            provider = FakeProvider()
            run_translation(
                source,
                root / "out.srt",
                None,
                root / "cache",
                "normal",
                provider,
                "m1",
                context_chars=45,
                polish=False,
                retries=1,
            )
            context_inputs = [
                call[1][-1]["content"]
                for call in provider.calls
                if call[1][0]["content"].startswith("为日语")
            ]
            self.assertGreater(len(context_inputs), 1)
            covered = "\n".join(context_inputs)
            for text in texts:
                self.assertEqual(1, covered.count(text))
            self.assertTrue(
                all(
                    not call[2]
                    for call in provider.calls
                    if call[1][0]["content"].startswith(("为日语", "合并分段"))
                )
            )
            self.assertTrue(
                all(
                    call[2]
                    for call in provider.calls
                    if not call[1][0]["content"].startswith(("为日语", "合并分段"))
                )
            )

            short = FakeProvider("fake:short")
            run_translation(
                source,
                root / "out-short.srt",
                None,
                root / "cache",
                "normal",
                short,
                "m1",
                context_chars=10_000,
                polish=False,
                retries=1,
            )
            self.assertEqual(
                1,
                sum(call[1][0]["content"].startswith("为日语") for call in short.calls),
            )
            self.assertFalse(
                any(call[1][0]["content"].startswith("合并分段") for call in short.calls)
            )

    def test_recursive_shrink_and_resume_only_missing_cached_cues(self) -> None:
        class InterruptingProvider(FakeProvider):
            def __init__(self, fail_id: int | None):
                super().__init__("fake:resume", max_items=2)
                self.fail_id = fail_id

            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                system = messages[0]["content"]
                if not system.startswith(("为日语", "合并分段")):
                    payload = _json_at_end(messages[-1]["content"])
                    ids = [item["id"] for item in payload["items"]]
                    if self.fail_id in ids:
                        self.calls.append(
                            (model, [dict(message) for message in messages], json_output)
                        )
                        return "{}"
                return super().chat(
                    messages,
                    model=model,
                    temperature=temperature,
                    json_output=json_output,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_source(root, ["a", "b", "c", "d"])
            first = InterruptingProvider(fail_id=3)
            output = root / "out.srt"
            with self.assertRaises(Exception):
                run_translation(
                    source,
                    output,
                    None,
                    root / "cache",
                    "normal",
                    first,
                    "m1",
                    use_global_context=False,
                    polish=False,
                    batch_size=4,
                    retries=1,
                )
            self.assertFalse(output.exists())

            resumed = InterruptingProvider(fail_id=None)
            run_translation(
                source,
                output,
                None,
                root / "cache",
                "normal",
                resumed,
                "m1",
                use_global_context=False,
                polish=False,
                batch_size=4,
                retries=1,
            )
            requested_ids = []
            for _, messages, _ in resumed.calls:
                requested_ids.extend(
                    item["id"] for item in _json_at_end(messages[-1]["content"])["items"]
                )
            self.assertEqual([3, 4], requested_ids)
            self.assertEqual(4, len(parse_srt(output)))

    def test_provider_outage_retries_without_recursive_batch_explosion(self) -> None:
        class OutageProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                self.calls.append(
                    (model, [dict(message) for message in messages], json_output)
                )
                raise RetryableProviderError("offline")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = OutageProvider()
            with self.assertRaises(Exception):
                run_translation(
                    _write_source(root, ["a", "b", "c", "d"]),
                    root / "out.srt",
                    None,
                    root / "cache",
                    "normal",
                    provider,
                    "m1",
                    use_global_context=False,
                    polish=False,
                    batch_size=4,
                    retries=2,
                )
            self.assertEqual(2, len(provider.calls))

    def test_cache_write_failure_does_not_repeat_paid_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = FakeProvider("fake:cache-write-failure")
            with mock.patch(
                "translation_flow.translation.pipeline._persist_batch",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(
                    TranslationPipelineError, "could not be cached"
                ):
                    run_translation(
                        _write_source(root, ["a", "b", "c", "d"]),
                        root / "out.srt",
                        None,
                        root / "cache",
                        "normal",
                        provider,
                        "m1",
                        use_global_context=False,
                        polish=False,
                        batch_size=4,
                        retries=1,
                        concurrency=1,
                    )
            self.assertEqual(1, len(provider.calls))

    def test_cache_isolated_by_profile_provider_model_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_source(root, ["a"])
            cache = root / "cache"
            first = FakeProvider("fake:A")
            kwargs = dict(use_global_context=False, polish=False, retries=1)
            run_translation(source, root / "one.srt", None, cache, "normal", first, "m1", **kwargs)

            reused = FakeProvider("fake:A")
            run_translation(source, root / "two.srt", None, cache, "normal", reused, "m1", **kwargs)
            self.assertEqual([], reused.calls)

            variants = [
                ("adult_direct", FakeProvider("fake:A"), "m1", source),
                ("normal", FakeProvider("fake:B"), "m1", source),
                ("normal", FakeProvider("fake:A"), "m2", source),
                ("normal", FakeProvider("fake:A"), "m1", _write_source(root, ["changed"], "changed.srt")),
            ]
            for number, (profile, provider, model, variant_source) in enumerate(variants):
                run_translation(
                    variant_source,
                    root / f"variant-{number}.srt",
                    None,
                    cache,
                    profile,
                    provider,
                    model,
                    **kwargs,
                )
                self.assertGreater(len(provider.calls), 0)

    def test_one_kana_repair_pass_reports_residue_without_looping(self) -> None:
        class KanaProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                self.calls.append(
                    (model, [dict(message) for message in messages], json_output)
                )
                system = messages[0]["content"]
                items = _json_at_end(messages[-1]["content"])["items"]
                if system.startswith("定点修复"):
                    text = "まだ"
                else:
                    text = "ｶﾅ"
                return json.dumps(
                    {"items": [{"id": item["id"], "text": text} for item in items]},
                    ensure_ascii=False,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_source(root, ["原文"])
            provider = KanaProvider()
            report = run_translation(
                source,
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "normal",
                provider,
                "m1",
                use_global_context=False,
                polish=False,
                retries=1,
            )
            repair_calls = [
                call for call in provider.calls if call[1][0]["content"].startswith("定点修复")
            ]
            self.assertEqual(1, len(repair_calls))
            self.assertTrue((root / "out.srt").is_file())
            self.assertTrue((root / "report.json").is_file())
            self.assertEqual([1], report["final_kana_residue_ids"])
            self.assertEqual(1, len(report["warnings"]))
            self.assertIn("ID：[1]", report["warnings"][0])

    def test_polish_prompt_has_exact_schema_and_accepts_known_v4_alias(self) -> None:
        class V4PolishAliasProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                self.calls.append(
                    (model, [dict(message) for message in messages], json_output)
                )
                system = messages[0]["content"]
                items = _json_at_end(messages[-1]["content"])["items"]
                if system.startswith("对照日文润色"):
                    self.test_case.assertIn(
                        '{"items":[{"id":1,"text":"..."}]}', system
                    )
                    self.test_case.assertIn("只能用原 ID 和 text 字段", system)
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "id": item["id"],
                                    "polished_text": f"润色-{item['zh']}",
                                }
                                for item in items
                            ]
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "items": [
                            {"id": item["id"], "text": f"初译-{item['text']}"}
                            for item in items
                        ]
                    },
                    ensure_ascii=False,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = V4PolishAliasProvider()
            provider.test_case = self
            run_translation(
                _write_source(root, ["原文"]),
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "normal",
                provider,
                "m1",
                use_global_context=False,
                retries=1,
            )
            self.assertEqual("润色-初译-原文", parse_srt(root / "out.srt")[0].text)

    def test_kana_repair_prompt_has_exact_schema_and_accepts_known_v4_alias(self) -> None:
        class V4RepairAliasProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                self.calls.append(
                    (model, [dict(message) for message in messages], json_output)
                )
                system = messages[0]["content"]
                items = _json_at_end(messages[-1]["content"])["items"]
                if system.startswith("定点修复"):
                    self.test_case.assertIn(
                        '{"items":[{"id":1,"text":"..."}]}', system
                    )
                    self.test_case.assertIn("只能用原 ID 和 text 字段", system)
                    return json.dumps(
                        {
                            "items": [
                                {"id": item["id"], "repaired_text": "修复后的中文"}
                                for item in items
                            ]
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "items": [
                            {"id": item["id"], "text": "かな"} for item in items
                        ]
                    },
                    ensure_ascii=False,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = V4RepairAliasProvider()
            provider.test_case = self
            run_translation(
                _write_source(root, ["原文"]),
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "normal",
                provider,
                "m1",
                use_global_context=False,
                polish=False,
                retries=1,
            )
            self.assertEqual("修复后的中文", parse_srt(root / "out.srt")[0].text)

    def test_adult_profile_explicitly_frames_adult_sexual_context(self) -> None:
        style = get_profile("adult_direct").style_instruction
        self.assertIn("All participants are adults", style)
        self.assertIn("pornography or an adult sexual-activity context", style)
        self.assertIn("faithfully", style)
        self.assertIn("sexually explicit", style)
        self.assertIn("without moralizing, euphemizing, or sanitizing", style)
        self.assertIn("moans", style)
        self.assertIn("Do not invent", style)

        normal_style = get_profile("normal").style_instruction
        self.assertNotIn("All participants are adults", normal_style)
        self.assertNotIn("pornography", normal_style)

    def test_empty_failure_publishes_neither_output_nor_report(self) -> None:
        class EmptyProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                items = _json_at_end(messages[-1]["content"])["items"]
                return json.dumps({"items": [{"id": item["id"], "text": ""} for item in items]})

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output, report = root / "out.srt", root / "report.json"
            with self.assertRaises(Exception):
                run_translation(
                    _write_source(root, ["a"]),
                    output,
                    report,
                    root / "cache",
                    "normal",
                    EmptyProvider(),
                    "m1",
                    use_global_context=False,
                    polish=False,
                    retries=1,
                )
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_repetition_collapse_publishes_neither_output_nor_report(self) -> None:
        class CollapsedProvider(FakeProvider):
            def chat(
                self,
                messages,
                *,
                model: str,
                temperature: float,
                json_output: bool = False,
            ) -> str:
                items = _json_at_end(messages[-1]["content"])["items"]
                return json.dumps(
                    {
                        "items": [
                            {"id": item["id"], "text": "完全相同的错误字幕"}
                            for item in items
                        ]
                    },
                    ensure_ascii=False,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output, report = root / "out.srt", root / "report.json"
            with self.assertRaisesRegex(TranslationValidationError, "repetition collapse"):
                run_translation(
                    _write_source(root, [f"原文{i}" for i in range(60)]),
                    output,
                    report,
                    root / "cache",
                    "normal",
                    CollapsedProvider(),
                    "m1",
                    use_global_context=False,
                    polish=False,
                    retries=1,
                )
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_existing_output_or_report_is_never_overwritten_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_source(root, ["a"])
            output = root / "out.srt"
            output.write_text("keep-output", encoding="utf-8")
            provider = FakeProvider()
            with self.assertRaises(FileExistsError):
                run_translation(
                    source,
                    output,
                    None,
                    root / "cache",
                    "normal",
                    provider,
                    "m1",
                    force=True,
                )
            self.assertEqual("keep-output", output.read_text(encoding="utf-8"))
            self.assertEqual([], provider.calls)

            output.unlink()
            report = root / "report.json"
            report.write_text("keep-report", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_translation(
                    source,
                    output,
                    report,
                    root / "cache",
                    "normal",
                    provider,
                    "m1",
                )
            self.assertFalse(output.exists())
            self.assertEqual("keep-report", report.read_text(encoding="utf-8"))
            self.assertEqual([], provider.calls)


class ProviderAndCliTests(unittest.TestCase):
    def test_legacy_cli_cannot_redirect_host_or_put_key_on_command_line(self) -> None:
        option_strings = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--api-base", option_strings)
        self.assertNotIn("--api-key", option_strings)
        self.assertIn("--consent-subtitle-text-to-deepseek", option_strings)

    def test_connection_check_sends_only_one_short_subtitle(self) -> None:
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"items": [{"id": 1, "text": "是"}]},
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def opener(request, timeout):
            requests.append(request)
            return Response()

        provider = DeepSeekProvider("secret-memory-key", opener=opener)
        provider.check_connection()
        self.assertEqual(1, len(requests))
        payload = json.loads(requests[0].data.decode("utf-8"))
        self.assertEqual(DEFAULT_DEEPSEEK_MODEL, payload["model"])
        self.assertEqual({"type": "enabled"}, payload["thinking"])
        self.assertEqual("high", payload["reasoning_effort"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual(32768, payload["max_tokens"])
        self.assertNotIn("temperature", payload)
        user_payload = json.loads(payload["messages"][1]["content"])
        self.assertEqual([{"id": 1, "text": "はい"}], user_payload["items"])
        self.assertNotIn("secret-memory-key", requests[0].data.decode("utf-8"))

    def test_flash_or_legacy_adapter_behavior_is_explicit(self) -> None:
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}
                ).encode("utf-8")

        def opener(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return Response()

        provider = DeepSeekProvider("secret", opener=opener)
        provider.chat(
            [], model="deepseek-v4-flash", temperature=0.3, json_output=True
        )
        provider.chat(
            [], model=DEFAULT_DEEPSEEK_MODEL, temperature=0.3, json_output=False
        )
        provider.chat([], model="legacy-test", temperature=0.3, json_output=False)

        self.assertEqual({"type": "enabled"}, requests[0]["thinking"])
        self.assertEqual({"type": "json_object"}, requests[0]["response_format"])
        self.assertNotIn("temperature", requests[0])
        self.assertEqual({"type": "enabled"}, requests[1]["thinking"])
        self.assertNotIn("response_format", requests[1])
        self.assertNotIn("max_tokens", requests[1])
        self.assertNotIn("temperature", requests[1])
        self.assertEqual(0.3, requests[2]["temperature"])
        self.assertNotIn("thinking", requests[2])
        self.assertNotIn("response_format", requests[2])

    def test_compatibility_cli_rejects_retired_deepseek_model(self) -> None:
        with self.assertRaises(SystemExit):
            cli_main(
                ["--input", "in.srt", "--output", "out.srt", "--model", "deepseek-chat"]
            )

    def test_cli_rejects_profile_plus_custom_style(self) -> None:
        with self.assertRaises(SystemExit):
            cli_main(
                [
                    "--input",
                    "in.srt",
                    "--output",
                    "out.srt",
                    "--profile",
                    "normal",
                    "--style-instruction",
                    "custom",
                    "--consent-subtitle-text-to-deepseek",
                ]
            )

    def test_api_key_is_not_persisted_in_output_cache_or_report(self) -> None:
        secret = "secret-memory-only-credential"

        class Response:
            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        def opener(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            items = _json_at_end(payload["messages"][-1]["content"])["items"]
            content = json.dumps(
                {"items": [{"id": item["id"], "text": "译文"} for item in items]},
                ensure_ascii=False,
            )
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            return Response(body)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = DeepSeekProvider(secret, opener=opener)
            run_translation(
                _write_source(root, ["原文"]),
                root / "out.srt",
                root / "report.json",
                root / "cache",
                "normal",
                provider,
                DEFAULT_DEEPSEEK_MODEL,
                use_global_context=False,
                polish=False,
                retries=1,
            )
            persisted = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)


if __name__ == "__main__":
    unittest.main()
