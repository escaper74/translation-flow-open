"""Plan one-video/all-audio soft-subtitle outputs from FFprobe facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from .source import MediaStream, SourceFacts


class OutputTarget(str, Enum):
    MKV = "mkv"
    MP4 = "mp4"


class StreamAction(str, Enum):
    COPY = "copy"
    CONVERT = "convert"
    REQUIRES_TRANSCODE = "requires_transcode"
    BLOCKED = "blocked"


class OutputPlanStatus(str, Enum):
    DIRECT_MUX = "direct_mux"
    REQUIRES_TRANSCODE = "requires_transcode"
    BLOCKED = "blocked"


class SubtitleSelectionError(ValueError):
    """The source does not identify exactly one Chinese subtitle stream."""


@dataclass(frozen=True)
class DirectMuxPolicy:
    """Known direct-mux failures; a real FFmpeg preflight is authoritative."""

    mkv_video_codecs_requiring_transcode: frozenset[str]
    mkv_audio_codecs_requiring_transcode: frozenset[str]
    mp4_video_codecs_requiring_transcode: frozenset[str]
    mp4_audio_codecs_requiring_transcode: frozenset[str]

    def codecs_requiring_transcode(
        self, target: OutputTarget, codec_type: str
    ) -> frozenset[str]:
        if target is OutputTarget.MKV:
            return (
                self.mkv_video_codecs_requiring_transcode
                if codec_type == "video"
                else self.mkv_audio_codecs_requiring_transcode
            )
        return (
            self.mp4_video_codecs_requiring_transcode
            if codec_type == "video"
            else self.mp4_audio_codecs_requiring_transcode
        )


# This is intentionally not an exhaustive codec matrix.  A positive allow-list
# would reject valid inputs before the installed FFmpeg build can prove them
# (for example ProRes + PCM into Matroska).  Only combinations established as
# incompatible are rejected here; every other plan still has to pass a short
# real mux with the exact copy/convert command shape.
# FFV1 -> MP4 is the pinned FFmpeg 8.1.1 fail-fast acceptance fixture.
CONSERVATIVE_DIRECT_MUX_POLICY = DirectMuxPolicy(
    mkv_video_codecs_requiring_transcode=frozenset(),
    mkv_audio_codecs_requiring_transcode=frozenset(),
    mp4_video_codecs_requiring_transcode=frozenset({"ffv1"}),
    mp4_audio_codecs_requiring_transcode=frozenset(),
)

TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"})
CHINESE_LANGUAGE_TAGS = frozenset({"zho", "chi", "cmn", "zh", "chs", "cht"})


@dataclass(frozen=True)
class PlannedStream:
    input_role: str
    index: int
    codec_type: str
    codec_name: str
    action: StreamAction
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_role": self.input_role,
            "index": self.index,
            "codec_type": self.codec_type,
            "codec_name": self.codec_name,
            "action": self.action.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OutputPlan:
    target: OutputTarget
    container: str
    extension: str
    subtitle_codec: str
    status: OutputPlanStatus
    video: PlannedStream
    audio: tuple[PlannedStream, ...]
    subtitle: PlannedStream
    uses_separate_audio_source: bool
    preserves_all_source_audio: bool
    requires_ffmpeg_preflight: bool
    reasons: tuple[str, ...]

    @property
    def burn_in(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.value,
            "container": self.container,
            "extension": self.extension,
            "subtitle_mode": "soft",
            "subtitle_codec": self.subtitle_codec,
            "burn_in": self.burn_in,
            "status": self.status.value,
            "video": self.video.to_dict(),
            "audio": [stream.to_dict() for stream in self.audio],
            "subtitle": self.subtitle.to_dict(),
            "uses_separate_audio_source": self.uses_separate_audio_source,
            "preserves_all_source_audio": self.preserves_all_source_audio,
            "requires_ffmpeg_preflight": self.requires_ffmpeg_preflight,
            "reasons": list(self.reasons),
        }


def _is_chinese_language(value: str) -> bool:
    normalized = value.strip().casefold().replace("_", "-")
    return normalized in CHINESE_LANGUAGE_TAGS or normalized.startswith("zh-")


def select_single_chinese_subtitle(source: SourceFacts) -> MediaStream:
    candidates = tuple(
        stream for stream in source.subtitle_streams if _is_chinese_language(stream.language)
    )
    if not candidates:
        raise SubtitleSelectionError(
            "FFprobe 没有找到标记为中文的字幕轨；首版不会根据文件名或字幕内容猜测"
        )
    if len(candidates) > 1:
        details = ", ".join(
            f"index={item.index}/codec={item.codec_name or 'unknown'}/language={item.language}"
            for item in candidates
        )
        raise SubtitleSelectionError(
            f"找到多条候选中文字幕（{details}）；当前入口不会静默选择"
        )
    return candidates[0]


def _plan_stream(
    stream: MediaStream,
    *,
    input_role: str,
    target: OutputTarget,
    policy: DirectMuxPolicy,
) -> PlannedStream:
    if not stream.codec_name:
        return PlannedStream(
            input_role=input_role,
            index=stream.index,
            codec_type=stream.codec_type,
            codec_name="",
            action=StreamAction.BLOCKED,
            reason=f"stream_{stream.index}_codec_unknown",
        )
    if stream.codec_name in policy.codecs_requiring_transcode(
        target, stream.codec_type
    ):
        return PlannedStream(
            input_role=input_role,
            index=stream.index,
            codec_type=stream.codec_type,
            codec_name=stream.codec_name,
            action=StreamAction.REQUIRES_TRANSCODE,
            reason=(
                f"{stream.codec_type}_{stream.codec_name}_cannot_be_directly_muxed_"
                f"under_{target.value}_policy"
            ),
        )
    return PlannedStream(
        input_role=input_role,
        index=stream.index,
        codec_type=stream.codec_type,
        codec_name=stream.codec_name,
        action=StreamAction.COPY,
    )


def _plan_subtitle(stream: MediaStream, target: OutputTarget) -> PlannedStream:
    if not stream.codec_name:
        return PlannedStream(
            input_role="subtitle_source",
            index=stream.index,
            codec_type=stream.codec_type,
            codec_name="",
            action=StreamAction.BLOCKED,
            reason=f"subtitle_stream_{stream.index}_codec_unknown",
        )
    if stream.codec_name not in TEXT_SUBTITLE_CODECS:
        return PlannedStream(
            input_role="subtitle_source",
            index=stream.index,
            codec_type=stream.codec_type,
            codec_name=stream.codec_name,
            action=StreamAction.BLOCKED,
            reason=f"subtitle_{stream.codec_name}_is_not_supported_text",
        )
    output_codec = "subrip" if target is OutputTarget.MKV else "mov_text"
    same_codec = stream.codec_name in (
        {"subrip", "srt"} if output_codec == "subrip" else {"mov_text"}
    )
    return PlannedStream(
        input_role="subtitle_source",
        index=stream.index,
        codec_type=stream.codec_type,
        codec_name=stream.codec_name,
        action=StreamAction.COPY if same_codec else StreamAction.CONVERT,
    )


def _selected_audio_streams(
    source: SourceFacts, indices: Sequence[int] | None
) -> tuple[MediaStream, ...]:
    available = source.audio_streams
    if indices is None:
        return available
    selected: list[MediaStream] = []
    seen: set[int] = set()
    for index in indices:
        if index in seen:
            raise ValueError(f"duplicate selected audio stream index: {index}")
        stream = source.stream(index)
        if stream.codec_type != "audio":
            raise ValueError(f"selected stream {index} is not audio")
        selected.append(stream)
        seen.add(index)
    return tuple(selected)


def plan_soft_subtitle_output(
    video_source: SourceFacts,
    *,
    selected_subtitle_index: int,
    audio_source: SourceFacts | None = None,
    target: OutputTarget | str,
    selected_video_index: int,
    audio_stream_indices: Sequence[int] | None = None,
    policy: DirectMuxPolicy = CONSERVATIVE_DIRECT_MUX_POLICY,
) -> OutputPlan:
    """Plan stream intent; the installed FFmpeg build must still prove it."""

    output_target = OutputTarget(target)
    video_stream = video_source.stream(selected_video_index)
    if video_stream.codec_type != "video" or video_stream.attached_picture:
        raise ValueError(f"selected stream {selected_video_index} is not a usable video")
    subtitle_stream = video_source.stream(selected_subtitle_index)
    if subtitle_stream.codec_type != "subtitle":
        raise ValueError(f"selected stream {selected_subtitle_index} is not a subtitle")
    audio_facts = audio_source if audio_source is not None else video_source
    audio_streams = _selected_audio_streams(audio_facts, audio_stream_indices)
    planned_video = _plan_stream(
        video_stream,
        input_role="video_source",
        target=output_target,
        policy=policy,
    )
    planned_audio = tuple(
        _plan_stream(
            stream,
            input_role="audio_source",
            target=output_target,
            policy=policy,
        )
        for stream in audio_streams
    )
    planned_subtitle = _plan_subtitle(subtitle_stream, output_target)
    planned_streams = (planned_video, *planned_audio, planned_subtitle)
    reasons = tuple(
        stream.reason for stream in planned_streams if stream.reason is not None
    )
    if any(stream.action is StreamAction.BLOCKED for stream in planned_streams):
        status = OutputPlanStatus.BLOCKED
    elif any(
        stream.action is StreamAction.REQUIRES_TRANSCODE
        for stream in (planned_video, *planned_audio)
    ):
        status = OutputPlanStatus.REQUIRES_TRANSCODE
    else:
        status = OutputPlanStatus.DIRECT_MUX

    all_audio_indices = tuple(stream.index for stream in audio_facts.audio_streams)
    selected_audio_indices = tuple(stream.index for stream in audio_streams)
    if output_target is OutputTarget.MKV:
        container, extension, subtitle_codec = "matroska", ".mkv", "subrip"
    else:
        container, extension, subtitle_codec = "mp4", ".mp4", "mov_text"
    preserves_all_audio = (
        len(selected_audio_indices) == len(all_audio_indices)
        and set(selected_audio_indices) == set(all_audio_indices)
    )
    if not preserves_all_audio:
        reasons = (*reasons, "not_all_source_audio_streams_selected")
        status = OutputPlanStatus.BLOCKED
    return OutputPlan(
        target=output_target,
        container=container,
        extension=extension,
        subtitle_codec=subtitle_codec,
        status=status,
        video=planned_video,
        audio=planned_audio,
        subtitle=planned_subtitle,
        uses_separate_audio_source=audio_source is not None,
        preserves_all_source_audio=preserves_all_audio,
        requires_ffmpeg_preflight=status is OutputPlanStatus.DIRECT_MUX,
        reasons=reasons,
    )
