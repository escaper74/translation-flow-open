from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from translation_flow.asr import AsrPipeline
from translation_flow.media_input import check_media_input
from tests import test_asr_packaging as asr_fixtures


def payload(format_name="matroska", video="hevc", audio="aac"):
    return {"format": {"format_name": format_name, "duration": "45"}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": video},
        {"index": 1, "codec_type": "audio", "codec_name": audio}]}


class MediaInputTests(unittest.TestCase):
    def run_check(self, root, data, *, failure=None, first_audio=True):
        source = root / "中文源片.unknown"
        source.write_bytes(b"unchanged-media")
        commands = []

        def process(args, **kwargs):
            commands.append(args)
            if args[0] == "ffprobe":
                return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
            kind = "decode" if args[-1] == "-" else "mux"
            if kind == "mux":
                Path(args[-1]).write_bytes(b"short-fixture")
            return subprocess.CompletedProcess(args, 1 if failure == kind else 0, "", "synthetic codec error" if failure == kind else "")

        result = check_media_input(source, root / "job", Path("ffmpeg"), Path("ffprobe"),
                                   process_runner=process, first_audio=first_audio)
        self.assertEqual(b"unchanged-media", source.read_bytes())
        self.assertEqual([], list((root / "job").glob(".input-check-*")))
        return result, commands

    def test_real_stream_facts_accept_mainstream_containers_without_extension_allowlist(self):
        for container, video, audio in (("mov,mp4", "h264", "aac"), ("matroska", "hevc", "ac3"),
                                         ("mov", "prores", "pcm_s16le"), ("avi", "mpeg4", "mp3"),
                                         ("asf", "wmv3", "wmav2")):
            with self.subTest(container=container), tempfile.TemporaryDirectory() as temp:
                result, commands = self.run_check(Path(temp), payload(container, video, audio))
                self.assertEqual("passed", result["status"])
                self.assertEqual(container.split(","), result["format_names"])
                self.assertEqual("copy", commands[-1][commands[-1].index("-c:v") + 1])
                self.assertEqual("copy", commands[-1][commands[-1].index("-c:a") + 1])
                self.assertIn("0:a:0", commands[1])
                self.assertIn("0:a", commands[2])

    def test_cover_is_not_main_video_and_all_audio_is_preserved(self):
        data = payload()
        data["streams"].insert(0, {"index": 8, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}})
        data["streams"].append({"index": 5, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 1}})
        with tempfile.TemporaryDirectory() as temp:
            report, commands = self.run_check(Path(temp), data)
            self.assertEqual(0, report["video_index"])
            self.assertEqual(1, report["asr_audio_index"])
            self.assertEqual([1, 5], report["audio_indices"])
            self.assertEqual([8], report["attached_picture_indices"])
            self.assertIn("0:V:0", commands[2])
            self.assertTrue(report["warnings"])

    def test_legacy_audio_policy_is_reported_without_claiming_a_uses_first_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            report, _ = self.run_check(Path(temp), payload(), first_audio=False)
            self.assertEqual("legacy", report["asr_audio_selection"])
            self.assertIsNone(report["asr_audio_index"])

    def test_invalid_streams_or_duration_stop_before_ffmpeg(self):
        cases = [payload() for _ in range(4)]
        cases[0]["streams"] = cases[0]["streams"][:1]
        cases[1]["streams"] = cases[1]["streams"][1:]
        cases[2]["format"]["duration"] = "nan"
        cases[3]["format"]["duration"] = "0"
        for data in cases:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source.mp4"
                source.write_bytes(b"fixture")
                process = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data), ""))
                with self.assertRaises(ValueError):
                    check_media_input(source, root / "job", Path("ffmpeg"), Path("ffprobe"), process_runner=process)
                self.assertEqual(1, process.call_count)

    def test_decode_or_mux_failure_preserves_source_and_cleans_only_owned_short_sample(self):
        for failure in ("decode", "mux"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with self.assertRaisesRegex(ValueError, "尚未启动 ASR/翻译"):
                    self.run_check(root, payload(), failure=failure)
                self.assertEqual(b"unchanged-media", (root / "中文源片.unknown").read_bytes())
                self.assertEqual([], list(root.rglob(".input-check-*")))

    def test_asr_does_not_start_when_media_preflight_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools = asr_fixtures.AsrPipelineTests()._tools(root)
            source = root / "source.avi"
            source.write_bytes(b"fixture")
            process = Mock(side_effect=AssertionError("ASR must not run"))
            with patch("translation_flow.asr.check_media_input", side_effect=ValueError("media incompatible")):
                with self.assertRaisesRegex(ValueError, "media incompatible"):
                    AsrPipeline(tools, process_runner=process).run(source, root / "job")
            process.assert_not_called()
