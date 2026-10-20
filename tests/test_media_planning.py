from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from media_planning import (
    FFprobeSourceProbe,
    OutputPlanStatus,
    OutputTarget,
    SourcePlanStatus,
    StreamAction,
    SubtitleSelectionError,
    parse_ffprobe_payload,
    plan_engine_input,
    plan_soft_subtitle_output,
    select_single_chinese_subtitle,
)


def _stream(
    index: int,
    codec_type: str,
    codec_name: str,
    *,
    default: bool = False,
    attached_picture: bool = False,
    language: str = "",
    title: str = "",
) -> dict:
    return {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec_name,
        "disposition": {
            "default": int(default),
            "attached_pic": int(attached_picture),
        },
        "tags": {"language": language, "title": title},
    }


def _payload(format_name: str, streams: list[dict]) -> dict:
    return {
        "format": {"format_name": format_name, "duration": "12.500"},
        "streams": streams,
    }


class SourcePlanningTests(unittest.TestCase):
    def test_probe_runner_reads_ffprobe_json_without_using_the_suffix(self) -> None:
        calls = []
        payload = _payload(
            "matroska,webm",
            [_stream(0, "video", "h264"), _stream(1, "audio", "aac")],
        )

        def runner(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(payload), stderr=""
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "renamed-with-unknown-suffix.data"
            source.write_bytes(b"synthetic placeholder; never decoded")
            facts = FFprobeSourceProbe("fake-ffprobe", process_runner=runner).probe(
                source
            )

        self.assertEqual(1, len(calls))
        self.assertIn("-show_streams", calls[0][0])
        self.assertEqual(str(source), calls[0][0][-1])
        self.assertEqual(SourcePlanStatus.PASS_THROUGH, plan_engine_input(facts).status)

    def test_container_labels_do_not_change_stream_decisions(self) -> None:
        formats = (
            "mov,mp4,m4a,3gp,3g2,mj2",
            "matroska,webm",
            "mpegts",
        )
        decisions = []
        for format_name in formats:
            facts = parse_ffprobe_payload(
                _payload(
                    format_name,
                    [
                        _stream(4, "video", "h264"),
                        _stream(7, "audio", "aac"),
                    ],
                )
            )
            plan = plan_engine_input(facts)
            decisions.append(
                (
                    plan.status,
                    plan.selected_video_index,
                    plan.selected_asr_audio_index,
                    plan.preserved_audio_indices,
                    plan.reasons,
                )
            )
        self.assertTrue(all(decision == decisions[0] for decision in decisions))

    def test_unverified_container_or_codec_requests_standardization(self) -> None:
        unknown_container = parse_ffprobe_payload(
            _payload(
                "unverified-container-name",
                [_stream(0, "video", "h264"), _stream(1, "audio", "aac")],
            )
        )
        unknown_codec = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [_stream(0, "video", "prores"), _stream(1, "audio", "pcm_s24le")],
            )
        )

        container_plan = plan_engine_input(unknown_container)
        codec_plan = plan_engine_input(unknown_codec)
        self.assertEqual(
            SourcePlanStatus.NEEDS_STANDARDIZATION, container_plan.status
        )
        self.assertIn("container_requires_standardization", container_plan.reasons)
        self.assertEqual(SourcePlanStatus.NEEDS_STANDARDIZATION, codec_plan.status)
        self.assertIn("video_codec_requires_standardization", codec_plan.reasons)
        self.assertIn("asr_audio_codec_requires_standardization", codec_plan.reasons)

    def test_attached_picture_is_not_selected_as_the_video(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "mjpeg", attached_picture=True),
                    _stream(1, "video", "hevc", default=True),
                    _stream(2, "audio", "aac"),
                ],
            )
        )
        plan = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.NEEDS_STANDARDIZATION, plan.status)
        self.assertEqual(1, plan.selected_video_index)
        self.assertIn("attached_picture_requires_standardization", plan.reasons)

    def test_unique_default_audio_is_selected_and_all_audio_is_preserved(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "mov,mp4,m4a,3gp,3g2,mj2",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "ac3", default=True),
                ],
            )
        )
        plan = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.NEEDS_STANDARDIZATION, plan.status)
        self.assertEqual(2, plan.selected_asr_audio_index)
        self.assertEqual((1, 2), plan.preserved_audio_indices)

    def test_ambiguous_audio_requires_selection_then_standardization(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "mpegts",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "aac"),
                ],
            )
        )
        ambiguous = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.NEEDS_SELECTION, ambiguous.status)
        self.assertIsNone(ambiguous.selected_asr_audio_index)
        self.assertIn(
            "multiple_audio_streams_require_selection", ambiguous.reasons
        )

        selected = plan_engine_input(facts, selected_asr_audio_index=2)
        self.assertEqual(SourcePlanStatus.NEEDS_STANDARDIZATION, selected.status)
        self.assertEqual(2, selected.selected_asr_audio_index)

    def test_ambiguous_video_requires_selection(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "video", "hevc"),
                    _stream(2, "audio", "aac"),
                ],
            )
        )
        plan = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.NEEDS_SELECTION, plan.status)
        self.assertIsNone(plan.selected_video_index)

    def test_multiple_video_streams_are_not_silently_selected_by_default(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264", default=True),
                    _stream(1, "video", "hevc"),
                    _stream(2, "audio", "aac"),
                ],
            )
        )
        plan = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.NEEDS_SELECTION, plan.status)
        self.assertIsNone(plan.selected_video_index)

    def test_missing_audio_cannot_feed_whisper(self) -> None:
        facts = parse_ffprobe_payload(
            _payload("matroska,webm", [_stream(0, "video", "h264")])
        )
        plan = plan_engine_input(facts)
        self.assertEqual(SourcePlanStatus.UNSUPPORTED, plan.status)
        self.assertIn("no_audio_stream_for_whisper", plan.reasons)


class OutputPlanningTests(unittest.TestCase):
    def test_selects_only_one_ffprobe_tagged_chinese_subtitle(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "subtitle", "subrip", language="eng"),
                    _stream(
                        3,
                        "subtitle",
                        "subrip",
                        language="zh-Hans",
                        title="简体中文",
                    ),
                ],
            )
        )
        selected = select_single_chinese_subtitle(facts)
        self.assertEqual(3, selected.index)
        self.assertEqual("zh-hans", selected.language)
        self.assertEqual("简体中文", selected.title)

    def test_zero_or_multiple_chinese_subtitles_fail_closed(self) -> None:
        no_chinese = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "subtitle", "subrip", language="eng"),
                ],
            )
        )
        multiple = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "subtitle", "subrip", language="zho", default=True),
                    _stream(2, "subtitle", "ass", language="chi"),
                ],
            )
        )
        with self.assertRaises(SubtitleSelectionError):
            select_single_chinese_subtitle(no_chinese)
        with self.assertRaises(SubtitleSelectionError):
            select_single_chinese_subtitle(multiple)

    def test_bitmap_chinese_subtitle_is_blocked(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "subtitle", "hdmv_pgs_subtitle", language="zho"),
                ],
            )
        )
        plan = plan_soft_subtitle_output(
            facts,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=2,
        )
        self.assertEqual(OutputPlanStatus.BLOCKED, plan.status)
        self.assertIn("not_supported_text", plan.reasons[0])

    def test_default_mkv_and_optional_mp4_choose_the_right_subtitle_codec(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "mpegts",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "ac3"),
                    _stream(3, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        mkv = plan_soft_subtitle_output(
            facts,
            target=OutputTarget.MKV,
            selected_video_index=0,
            selected_subtitle_index=3,
        )
        mp4 = plan_soft_subtitle_output(
            facts,
            target=OutputTarget.MP4,
            selected_video_index=0,
            selected_subtitle_index=3,
        )

        self.assertEqual(OutputPlanStatus.DIRECT_MUX, mkv.status)
        self.assertEqual("subrip", mkv.subtitle_codec)
        self.assertEqual(".mkv", mkv.extension)
        self.assertEqual(OutputPlanStatus.DIRECT_MUX, mp4.status)
        self.assertEqual("mov_text", mp4.subtitle_codec)
        self.assertEqual(".mp4", mp4.extension)
        self.assertTrue(mkv.preserves_all_source_audio)
        self.assertTrue(mp4.preserves_all_source_audio)
        self.assertFalse(mkv.burn_in)
        self.assertTrue(mkv.requires_ffmpeg_preflight)
        self.assertEqual(StreamAction.COPY, mkv.subtitle.action)
        self.assertEqual(StreamAction.CONVERT, mp4.subtitle.action)

    def test_unlisted_codecs_reach_the_real_ffmpeg_preflight(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "mov,mp4,m4a,3gp,3g2,mj2",
                [
                    _stream(0, "video", "prores"),
                    _stream(1, "audio", "pcm_s24le"),
                    _stream(2, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        mkv = plan_soft_subtitle_output(
            facts,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=2,
        )
        mp4 = plan_soft_subtitle_output(
            facts,
            target="mp4",
            selected_video_index=0,
            selected_subtitle_index=2,
        )

        self.assertEqual(OutputPlanStatus.DIRECT_MUX, mkv.status)
        self.assertEqual(OutputPlanStatus.DIRECT_MUX, mp4.status)
        self.assertEqual(StreamAction.COPY, mkv.video.action)
        self.assertEqual(StreamAction.COPY, mkv.audio[0].action)
        self.assertTrue(mkv.requires_ffmpeg_preflight)
        self.assertTrue(mp4.requires_ffmpeg_preflight)

    def test_known_ffv1_to_mp4_stops_before_the_real_preflight(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "ffv1"),
                    _stream(1, "audio", "pcm_s24le"),
                    _stream(2, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        plan = plan_soft_subtitle_output(
            facts,
            target="mp4",
            selected_video_index=0,
            selected_subtitle_index=2,
        )

        self.assertEqual(OutputPlanStatus.REQUIRES_TRANSCODE, plan.status)
        self.assertEqual(StreamAction.REQUIRES_TRANSCODE, plan.video.action)
        self.assertEqual(StreamAction.COPY, plan.audio[0].action)

    def test_processed_video_can_be_planned_with_original_source_audio(self) -> None:
        processed_video = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "hevc"),
                    _stream(1, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        original_source = parse_ffprobe_payload(
            _payload(
                "mov,mp4,m4a,3gp,3g2,mj2",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "ac3"),
                ],
            )
        )

        plan = plan_soft_subtitle_output(
            processed_video,
            audio_source=original_source,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=1,
        )

        self.assertEqual(OutputPlanStatus.DIRECT_MUX, plan.status)
        self.assertTrue(plan.uses_separate_audio_source)
        self.assertTrue(plan.preserves_all_source_audio)
        self.assertEqual("video_source", plan.video.input_role)
        self.assertEqual(
            ["audio_source", "audio_source"],
            [stream.input_role for stream in plan.audio],
        )
        self.assertEqual([1, 2], [stream.index for stream in plan.audio])

    def test_unknown_codec_blocks_instead_of_guessing(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "unverified-container",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", ""),
                    _stream(2, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        plan = plan_soft_subtitle_output(
            facts,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=2,
        )
        self.assertEqual(OutputPlanStatus.BLOCKED, plan.status)
        self.assertEqual(StreamAction.BLOCKED, plan.audio[0].action)

    def test_audio_subset_is_explicitly_reported_as_not_preserving_all_audio(self) -> None:
        facts = parse_ffprobe_payload(
            _payload(
                "matroska,webm",
                [
                    _stream(0, "video", "h264"),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "ac3"),
                    _stream(3, "subtitle", "subrip", language="zho"),
                ],
            )
        )
        plan = plan_soft_subtitle_output(
            facts,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=3,
            audio_stream_indices=(2,),
        )
        reordered_all = plan_soft_subtitle_output(
            facts,
            target="mkv",
            selected_video_index=0,
            selected_subtitle_index=3,
            audio_stream_indices=(2, 1),
        )
        self.assertFalse(plan.preserves_all_source_audio)
        self.assertEqual([2], [stream.index for stream in plan.audio])
        self.assertTrue(reordered_all.preserves_all_source_audio)


if __name__ == "__main__":
    unittest.main()
