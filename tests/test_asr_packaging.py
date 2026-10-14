from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from translation_flow.asr import AsrPipeline, ToolPaths, conservative_fuse
from translation_flow.packaging import MkvSoftSubtitlePackager
from translation_flow.srt import SubtitleCue, write_srt
from scripts.clean_whisper_srt import Block, clean_blocks
from scripts.asr_fusion.validate_asr_srt import (
    Block as StructureBlock,
    validate_blocks,
)


def _completed(args: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def _input_preflight(args, tools):
    if args[0] == str(tools.ffprobe):
        return _completed(args, stdout=json.dumps({"format": {"duration": "10.0", "format_name": "matroska"},
                          "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"},
                                      {"index": 1, "codec_type": "audio", "codec_name": "aac"}]}))
    if args[0] == str(tools.ffmpeg):
        if args[-1] != "-":
            Path(args[-1]).write_bytes(b"fake-mux-preflight")
        return _completed(args)
    return None


class ConservativeFusionTests(unittest.TestCase):
    def test_only_unique_textually_consistent_c_replaces_a(self) -> None:
        a = [
            SubtitleCue(1, 0, 1000, "こんにちは"),
            SubtitleCue(2, 1000, 2000, "まったく違う"),
        ]
        c = [
            SubtitleCue(1, 50, 950, "こんにちは。"),
            SubtitleCue(2, 1050, 1950, "別の候補です"),
        ]

        fused, matches = conservative_fuse(a, c)

        self.assertEqual([cue.text for cue in fused], ["こんにちは。", "まったく違う"])
        self.assertEqual(
            [(cue.index, cue.start_ms, cue.end_ms) for cue in fused],
            [(cue.index, cue.start_ms, cue.end_ms) for cue in a],
        )
        self.assertTrue(matches[0].accepted)
        self.assertFalse(matches[1].accepted)

    def test_ambiguous_c_segmentation_is_rejected(self) -> None:
        a = [SubtitleCue(1, 0, 2000, "長い発話です")]
        c = [
            SubtitleCue(1, 0, 1000, "長い発話"),
            SubtitleCue(2, 1000, 2000, "です"),
        ]

        fused, matches = conservative_fuse(a, c)

        self.assertEqual(fused[0].text, a[0].text)
        self.assertTrue(matches)
        self.assertTrue(all(not match.accepted for match in matches))

    def test_zero_duration_backbone_is_rejected(self) -> None:
        a = [
            SubtitleCue(1, 500, 500, "短い声"),
            SubtitleCue(2, 500, 1200, "続き"),
        ]
        c = [SubtitleCue(1, 500, 900, "短い声")]

        with self.assertRaisesRegex(ValueError, "Whisper A.*non-positive-duration"):
            conservative_fuse(a, c)


class AsrPipelineTests(unittest.TestCase):
    def test_cleaner_drops_all_unpresentable_intervals_and_records_reason(self) -> None:
        blocks = [
            Block(1, "01:58:05,040", "01:58:09,020", ["おじいね"]),
            Block(2, "01:58:09,020", "01:58:09,020", ["おじいね"]),
            Block(3, "01:58:09,020", "01:58:09,020", ["別の短い声"]),
            Block(4, "01:58:09,020", "01:58:36,060", ["すごい"]),
            Block(5, "01:58:36,060", "01:58:36,060", ["次の声"]),
            Block(6, "01:58:36,060", "01:58:38,000", ["次の声"]),
            Block(7, "01:58:39,000", "01:58:38,000", ["逆向き"]),
            Block(8, "01:58:40,000", "01:58:41,000", ["  "]),
        ]

        kept, dropped = clean_blocks(blocks)

        self.assertEqual([1, 4, 6], [block.index for block in kept])
        self.assertEqual(
            [
                {
                    "index": 2,
                    "start": "01:58:09,020",
                    "end": "01:58:09,020",
                    "text": "おじいね",
                    "reason": "zero-duration exact duplicate at adjacent boundary",
                },
                {
                    "index": 3,
                    "start": "01:58:09,020",
                    "end": "01:58:09,020",
                    "text": "別の短い声",
                    "reason": "zero-duration cue cannot be displayed",
                },
                {
                    "index": 5,
                    "start": "01:58:36,060",
                    "end": "01:58:36,060",
                    "text": "次の声",
                    "reason": "zero-duration exact duplicate at adjacent boundary",
                },
                {
                    "index": 7,
                    "start": "01:58:39,000",
                    "end": "01:58:38,000",
                    "text": "逆向き",
                    "reason": "negative-duration cue cannot be displayed",
                },
                {
                    "index": 8,
                    "start": "01:58:40,000",
                    "end": "01:58:41,000",
                    "text": "",
                    "reason": "empty cue cannot be displayed",
                },
            ],
            dropped,
        )

    def test_structure_gate_keeps_integrity_failures_but_warns_on_presentation(self) -> None:
        report = validate_blocks(
            [
                StructureBlock(1, 0.0, 2.0, ("正常",)),
                StructureBlock(2, 1.9, 11.0, ("这一行很长", "第二行", "第三行")),
            ],
            source_duration=10.0,
            max_lines=2,
            max_line_chars=4,
            max_cue_duration=6.0,
        )

        self.assertEqual([], report["failures"])
        self.assertTrue(any("overlap" in item for item in report["warnings"]))
        self.assertTrue(any("exceeds source duration" in item for item in report["warnings"]))
        self.assertTrue(any("has 3 lines" in item for item in report["warnings"]))
        self.assertTrue(any("visible chars" in item for item in report["warnings"]))
        self.assertTrue(any("lasts" in item for item in report["warnings"]))

        invalid = validate_blocks(
            [StructureBlock(2, 1.0, 1.0, ("",))],
            source_duration=10.0,
        )
        self.assertTrue(invalid["failures"])

    def _tools(self, root: Path) -> ToolPaths:
        names = {
            "python": root / "python.exe",
            "faster_python": root / "faster-python.exe",
            "ffmpeg": root / "ffmpeg.exe",
            "ffprobe": root / "ffprobe.exe",
            "whisper": root / "whisper.exe",
            "whisper_model": root / "large-v3.bin",
            "faster_model": root / "faster-model",
            "transcribe_a": root / "a.py",
            "clean_srt": root / "clean.py",
            "validate_quality": root / "quality.py",
            "transcribe_c": root / "c.py",
            "validate_structure": root / "structure.py",
        }
        for name, path in names.items():
            if name == "faster_model":
                path.mkdir()
            else:
                path.write_text("test", encoding="utf-8")
        return ToolPaths(**names)

    def test_a_exits_before_c_and_fusion_keeps_a_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            calls: list[list[str]] = []

            def runner(args: list[str], **_: object):
                calls.append(args)
                preflight = _input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if len(args) > 1 and args[1] == str(tools.transcribe_a):
                    Path(args[args.index("--raw-output-srt") + 1]).write_text(
                        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
                        encoding="utf-8",
                    )
                    Path(args[args.index("--output-srt") + 1]).write_text(
                        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
                        encoding="utf-8",
                    )
                elif args[0] == str(tools.faster_python):
                    Path(args[args.index("--output-srt") + 1]).write_text(
                        "1\n00:00:00,050 --> 00:00:00,950\nこんにちは。\n",
                        encoding="utf-8",
                    )
                elif len(args) > 1 and args[1] == str(tools.clean_srt):
                    input_path = Path(args[args.index("--input") + 1])
                    output_path = Path(args[args.index("--output") + 1])
                    output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
                elif len(args) > 1 and args[1] in {
                    str(tools.validate_quality),
                    str(tools.validate_structure),
                }:
                    report = Path(args[args.index("--report") + 1])
                    warnings = (
                        ["playable presentation warning"]
                        if args[1] == str(tools.validate_structure)
                        else []
                    )
                    report.write_text(
                        json.dumps({"failures": [], "warnings": warnings}),
                        encoding="utf-8",
                    )
                return _completed(args)

            job_root = root / "中文任务"
            result, report = AsrPipeline(tools, process_runner=runner).run(
                source, job_root
            )

            a_position = next(i for i, call in enumerate(calls) if len(call) > 1 and call[1] == str(tools.transcribe_a))
            c_position = next(i for i, call in enumerate(calls) if call[0] == str(tools.faster_python))
            self.assertLess(a_position, c_position)
            self.assertIn("--first-audio", calls[a_position])
            self.assertEqual(report["c_replacements"], 1)
            self.assertTrue(report["timeline_preserved_from_a"])
            self.assertEqual(
                ["playable presentation warning"],
                report["structure_warnings"]["A"],
            )
            self.assertIn("00:00:00,000 --> 00:00:01,000", result.read_text(encoding="utf-8-sig"))
            transcribe_a = next(
                call for call in calls if len(call) > 1 and call[1] == str(tools.transcribe_a)
            )
            a_work_dir = transcribe_a[transcribe_a.index("--work-dir") + 1]
            self.assertTrue(a_work_dir.isascii())
            self.assertNotIn(str(job_root), a_work_dir)
            self.assertTrue(str(Path(a_work_dir)).startswith(str(root)))

    def test_zero_duration_a_stops_before_c_and_fusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            calls: list[list[str]] = []

            def runner(args: list[str], **_: object):
                calls.append(args)
                preflight = _input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if len(args) > 1 and args[1] == str(tools.transcribe_a):
                    value = "1\n00:00:00,500 --> 00:00:00,500\n声\n"
                    Path(args[args.index("--raw-output-srt") + 1]).write_text(value, encoding="utf-8")
                    Path(args[args.index("--output-srt") + 1]).write_text(value, encoding="utf-8")
                elif args[0] == str(tools.faster_python):
                    Path(args[args.index("--output-srt") + 1]).write_text(
                        "1\n00:00:00,500 --> 00:00:00,900\n声\n", encoding="utf-8"
                    )
                elif len(args) > 1 and args[1] == str(tools.clean_srt):
                    Path(args[args.index("--output") + 1]).write_text(
                        Path(args[args.index("--input") + 1]).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                return _completed(args)

            with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                AsrPipeline(tools, process_runner=runner).run(source, root / "job")

            self.assertFalse(any(call[0] == str(tools.faster_python) for call in calls))
            self.assertFalse(
                any(
                    len(call) > 1 and call[1] == str(tools.validate_structure)
                    for call in calls
                )
            )

    def test_old_stage_artifacts_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            stale = root / "job" / "A" / "chunks" / "chunk_0001.srt"
            stale.parent.mkdir(parents=True)
            stale.write_text("old source", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "old ASR stage artifacts"):
                AsrPipeline(tools).run(source, root / "job")

    def test_completed_asr_checkpoint_resumes_without_model_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = (root / "source.mkv").resolve()
            source.write_bytes(b"media")
            job = root / "job"
            value = "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n"
            for relative in ("A/A.ja.srt", "C/C.ja.srt", "fusion/fusion.ja.srt"):
                path = job / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
            report = {
                "status": "complete",
                "source": str(source),
                "source_size_bytes": source.stat().st_size,
                "source_mtime_ns": source.stat().st_mtime_ns,
                "a_cues": 1,
                "c_cues": 1,
                "fusion_cues": 1,
                "timeline_preserved_from_a": True,
            }
            (job / "asr.report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            calls: list[list[str]] = []

            result, resumed = AsrPipeline(
                tools, process_runner=lambda args, **kwargs: calls.append(args)
            ).run(source, job)

            self.assertEqual(job / "fusion" / "fusion.ja.srt", result)
            self.assertEqual(report, resumed)
            self.assertEqual([], calls)

    def test_partial_checkpoint_rebuilds_a_final_from_resumable_chunk_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = (root / "source.mkv").resolve()
            source.write_bytes(b"media")
            job = root / "job"
            a_srt = job / "A" / "A.ja.srt"
            a_srt.parent.mkdir(parents=True)
            a_srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
                encoding="utf-8",
            )
            (job / "asr.input.json").write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "source_size_bytes": source.stat().st_size,
                        "source_mtime_ns": source.stat().st_mtime_ns,
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def runner(args: list[str], **_: object):
                calls.append(args)
                preflight = _input_preflight(args, tools)
                if preflight is not None:
                    return preflight
                if len(args) > 1 and args[1] == str(tools.transcribe_a):
                    value = "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n"
                    Path(args[args.index("--raw-output-srt") + 1]).write_text(
                        value, encoding="utf-8"
                    )
                    Path(args[args.index("--output-srt") + 1]).write_text(
                        value, encoding="utf-8"
                    )
                elif args[0] == str(tools.faster_python):
                    Path(args[args.index("--output-srt") + 1]).write_text(
                        "1\n00:00:00,050 --> 00:00:00,950\nこんにちは。\n",
                        encoding="utf-8",
                    )
                elif len(args) > 1 and args[1] == str(tools.clean_srt):
                    Path(args[args.index("--output") + 1]).write_text(
                        Path(args[args.index("--input") + 1]).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                elif len(args) > 1 and args[1] in {
                    str(tools.validate_quality),
                    str(tools.validate_structure),
                }:
                    Path(args[args.index("--report") + 1]).write_text(
                        '{"failures": []}', encoding="utf-8"
                    )
                return _completed(args)

            result, _ = AsrPipeline(tools, process_runner=runner).run(source, job)
            a_call = next(call for call in calls if len(call) > 1 and call[1] == str(tools.transcribe_a))
            self.assertNotIn("--first-audio", a_call)

            self.assertTrue(result.is_file())
            self.assertTrue(
                any(len(call) > 1 and call[1] == str(tools.transcribe_a) for call in calls)
            )
            self.assertTrue(any(call[0] == str(tools.faster_python) for call in calls))

    def test_partial_checkpoint_rejects_replaced_file_at_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = self._tools(root)
            source = (root / "source.mkv").resolve()
            source.write_bytes(b"old-media")
            old_stat = source.stat()
            job = root / "job"
            job.mkdir()
            (job / "asr.input.json").write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "source_size_bytes": old_stat.st_size,
                        "source_mtime_ns": old_stat.st_mtime_ns,
                    }
                ),
                encoding="utf-8",
            )
            source.write_bytes(b"different-media-size")

            with self.assertRaisesRegex(ValueError, "different source"):
                AsrPipeline(
                    tools,
                    process_runner=lambda *_args, **_kwargs: self.fail(
                        "no local tool should run for a mismatched checkpoint"
                    ),
                ).run(source, job)


class PackagingTests(unittest.TestCase):
    @staticmethod
    def _probe(*, subtitle: bool, duration: float = 30.0) -> dict[str, object]:
        streams: list[dict[str, object]] = [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "ac3"},
        ]
        if subtitle:
            streams.append(
                {
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "zho"},
                    "disposition": {"default": 1},
                }
            )
        return {
            "format": {"duration": str(duration), "format_name": "matroska,webm"},
            "streams": streams,
        }

    def test_container_duration_offset_warns_before_it_becomes_a_hard_failure(self) -> None:
        source = self._probe(subtitle=False, duration=30.0)
        accepted = MkvSoftSubtitlePackager._validate(
            source,
            self._probe(subtitle=True, duration=31.0),
        )
        self.assertEqual(1, len(accepted["warnings"]))

        with self.assertRaisesRegex(ValueError, "duration_preserved"):
            MkvSoftSubtitlePackager._validate(
                source,
                self._probe(subtitle=True, duration=32.1),
            )

    def test_mux_maps_primary_video_all_audio_and_new_subtitle_then_checks_three_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg, ffprobe = root / "ffmpeg.exe", root / "ffprobe.exe"
            ffmpeg.write_bytes(b"tool")
            ffprobe.write_bytes(b"tool")
            source, subtitle = root / "source.mkv", root / "zh.srt"
            source.write_bytes(b"media")
            write_srt(subtitle, [SubtitleCue(1, 0, 1000, "中文")])
            output, report = root / "final.mkv", root / "report.json"
            calls: list[list[str]] = []

            def runner(args: list[str], **_: object):
                calls.append(args)
                if args[0] == str(ffprobe):
                    is_candidate = ".candidate.mkv" in args[-1]
                    return _completed(args, stdout=json.dumps(self._probe(subtitle=is_candidate)))
                if "-c:s" in args:
                    Path(args[-1]).write_bytes(b"candidate")
                return _completed(args)

            result = MkvSoftSubtitlePackager(
                ffmpeg, ffprobe, process_runner=runner
            ).package(source, subtitle, output, report)

            mux = next(call for call in calls if "-c:s" in call)
            self.assertIn("-n", mux)
            self.assertEqual(
                [mux[index + 1] for index, value in enumerate(mux) if value == "-map"],
                ["0:V:0", "0:a?", "1:0"],
            )
            self.assertEqual(mux[mux.index("-c:v") + 1], "copy")
            self.assertEqual(mux[mux.index("-c:a") + 1], "copy")
            self.assertEqual(result, output)
            self.assertTrue(output.is_file())
            short_decodes = [call for call in calls if "-xerror" in call]
            self.assertEqual(len(short_decodes), 3)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["short_decode_positions_seconds"], [0.0, 14.5, 29.0])

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("ffmpeg.exe", "ffprobe.exe", "source.mkv", "zh.srt", "final.mkv")]
            for path in paths:
                path.write_bytes(b"keep")
            packager = MkvSoftSubtitlePackager(paths[0], paths[1])
            with self.assertRaises(FileExistsError):
                packager.package(paths[2], paths[3], paths[4], root / "report.json")
            self.assertEqual(paths[4].read_bytes(), b"keep")

    def test_report_publish_failure_rolls_back_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg, ffprobe = root / "ffmpeg.exe", root / "ffprobe.exe"
            ffmpeg.write_bytes(b"tool")
            ffprobe.write_bytes(b"tool")
            source, subtitle = root / "source.mkv", root / "zh.srt"
            source.write_bytes(b"media")
            write_srt(subtitle, [SubtitleCue(1, 0, 1000, "中文")])
            output, report = root / "final.mkv", root / "report.json"

            def runner(args: list[str], **_: object):
                if args[0] == str(ffprobe):
                    return _completed(
                        args,
                        stdout=json.dumps(
                            self._probe(subtitle=".candidate.mkv" in args[-1])
                        ),
                    )
                if "-c:s" in args:
                    Path(args[-1]).write_bytes(b"candidate")
                return _completed(args)

            import translation_flow.packaging as packaging_module

            real_publish = packaging_module._publish_exclusive
            publish_calls = 0

            def fail_second_publish(source_path: Path, destination_path: Path) -> None:
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 2:
                    raise OSError("simulated report publish failure")
                real_publish(source_path, destination_path)

            original_publish = packaging_module._publish_exclusive
            packaging_module._publish_exclusive = fail_second_publish
            try:
                with self.assertRaisesRegex(OSError, "report publish failure"):
                    MkvSoftSubtitlePackager(
                        ffmpeg, ffprobe, process_runner=runner
                    ).package(source, subtitle, output, report)
            finally:
                packaging_module._publish_exclusive = original_publish

            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_zero_duration_chinese_input_is_rejected_before_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg, ffprobe = root / "ffmpeg.exe", root / "ffprobe.exe"
            ffmpeg.write_bytes(b"tool")
            ffprobe.write_bytes(b"tool")
            source, subtitle = root / "source.mkv", root / "zh.srt"
            source.write_bytes(b"media")
            subtitle.write_text(
                "1\n00:00:00,500 --> 00:00:00,500\n中文\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            with self.assertRaisesRegex(ValueError, "non-positive-duration"):
                MkvSoftSubtitlePackager(
                    ffmpeg,
                    ffprobe,
                    process_runner=lambda args, **kwargs: calls.append(args),
                ).package(
                    source,
                    subtitle,
                    root / "final.mkv",
                    root / "report.json",
                )

            self.assertEqual([], calls)
            self.assertFalse((root / "final.mkv").exists())
            self.assertFalse((root / "report.json").exists())

    def test_validation_requires_matroska_and_default_chinese_subtitle(self) -> None:
        source = self._probe(subtitle=False)
        output = self._probe(subtitle=True)
        output["format"]["format_name"] = "mov,mp4"
        with self.assertRaisesRegex(ValueError, "matroska_container"):
            MkvSoftSubtitlePackager._validate(source, output)

        output = self._probe(subtitle=True)
        output["streams"][-1]["disposition"]["default"] = 0
        with self.assertRaisesRegex(ValueError, "subtitle_default"):
            MkvSoftSubtitlePackager._validate(source, output)


if __name__ == "__main__":
    unittest.main()
