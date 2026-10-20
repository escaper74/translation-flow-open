"""Inspect FFprobe stream facts and plan inputs for the unchanged engines."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class SourceProbeError(RuntimeError):
    """FFprobe did not provide a trustworthy source description."""


class SourcePlanStatus(str, Enum):
    PASS_THROUGH = "pass_through"
    NEEDS_STANDARDIZATION = "needs_standardization"
    NEEDS_SELECTION = "needs_selection"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class EngineInputPolicy:
    """Small initial pass-through policy, not an exhaustive support matrix."""

    format_names: frozenset[str]
    video_codecs: frozenset[str]
    asr_audio_codecs: frozenset[str]


INITIAL_ENGINE_INPUT_POLICY = EngineInputPolicy(
    format_names=frozenset({"mov", "mp4", "matroska", "mpegts"}),
    video_codecs=frozenset({"h264", "hevc"}),
    asr_audio_codecs=frozenset({"aac", "ac3", "eac3", "mp3"}),
)


@dataclass(frozen=True)
class MediaStream:
    index: int
    codec_type: str
    codec_name: str
    default: bool = False
    attached_picture: bool = False
    language: str = ""
    title: str = ""


@dataclass(frozen=True)
class SourceFacts:
    """Facts reported by FFprobe; a filename extension is deliberately absent."""

    format_names: tuple[str, ...]
    streams: tuple[MediaStream, ...]
    duration_seconds: float | None = None

    def streams_of_type(self, codec_type: str) -> tuple[MediaStream, ...]:
        wanted = codec_type.casefold()
        return tuple(stream for stream in self.streams if stream.codec_type == wanted)

    @property
    def video_candidates(self) -> tuple[MediaStream, ...]:
        return tuple(
            stream
            for stream in self.streams_of_type("video")
            if not stream.attached_picture
        )

    @property
    def attached_pictures(self) -> tuple[MediaStream, ...]:
        return tuple(
            stream
            for stream in self.streams_of_type("video")
            if stream.attached_picture
        )

    @property
    def audio_streams(self) -> tuple[MediaStream, ...]:
        return self.streams_of_type("audio")

    @property
    def subtitle_streams(self) -> tuple[MediaStream, ...]:
        return self.streams_of_type("subtitle")

    def stream(self, index: int) -> MediaStream:
        for stream in self.streams:
            if stream.index == index:
                return stream
        raise ValueError(f"source has no stream index {index}")


@dataclass(frozen=True)
class SourcePlan:
    status: SourcePlanStatus
    selected_video_index: int | None
    selected_asr_audio_index: int | None
    preserved_audio_indices: tuple[int, ...]
    format_names: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "selected_video_index": self.selected_video_index,
            "selected_asr_audio_index": self.selected_asr_audio_index,
            "preserved_audio_indices": list(self.preserved_audio_indices),
            "format_names": list(self.format_names),
            "reasons": list(self.reasons),
        }


def _flag(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip() == "1"


def _stream_from_mapping(value: Mapping[str, Any]) -> MediaStream:
    try:
        index = int(value["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceProbeError("FFprobe stream is missing a valid index") from exc
    if index < 0:
        raise SourceProbeError(f"FFprobe stream index cannot be negative: {index}")
    codec_type = str(value.get("codec_type") or "").strip().casefold()
    if not codec_type:
        raise SourceProbeError(f"FFprobe stream {index} is missing codec_type")
    codec_name = str(value.get("codec_name") or "").strip().casefold()
    disposition = value.get("disposition")
    if not isinstance(disposition, Mapping):
        disposition = {}
    tags = value.get("tags")
    if not isinstance(tags, Mapping):
        tags = {}
    return MediaStream(
        index=index,
        codec_type=codec_type,
        codec_name=codec_name,
        default=_flag(disposition.get("default", 0)),
        attached_picture=_flag(disposition.get("attached_pic", 0)),
        language=str(tags.get("language") or "").strip().casefold(),
        title=str(tags.get("title") or "").strip(),
    )


def _optional_duration(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def parse_ffprobe_payload(payload: Mapping[str, Any]) -> SourceFacts:
    """Parse FFprobe JSON without inferring anything from a source filename."""

    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, Sequence) or isinstance(raw_streams, (str, bytes)):
        raise SourceProbeError("FFprobe payload must contain a streams array")
    streams: list[MediaStream] = []
    seen_indices: set[int] = set()
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, Mapping):
            raise SourceProbeError("every FFprobe stream must be an object")
        stream = _stream_from_mapping(raw_stream)
        if stream.index in seen_indices:
            raise SourceProbeError(f"duplicate FFprobe stream index: {stream.index}")
        streams.append(stream)
        seen_indices.add(stream.index)

    raw_format = payload.get("format")
    if not isinstance(raw_format, Mapping):
        raw_format = {}
    format_names = tuple(
        name.strip().casefold()
        for name in str(raw_format.get("format_name") or "").split(",")
        if name.strip()
    )
    return SourceFacts(
        format_names=format_names,
        streams=tuple(streams),
        duration_seconds=_optional_duration(raw_format.get("duration")),
    )


def _resolve_selected_stream(
    candidates: Sequence[MediaStream],
    explicit_index: int | None,
    *,
    label: str,
    allow_unique_default: bool,
) -> tuple[MediaStream | None, bool]:
    if explicit_index is not None:
        for stream in candidates:
            if stream.index == explicit_index:
                return stream, False
        raise ValueError(f"selected {label} stream {explicit_index} is not a candidate")
    if len(candidates) == 1:
        return candidates[0], False
    if allow_unique_default:
        defaults = [stream for stream in candidates if stream.default]
        if len(defaults) == 1:
            return defaults[0], False
    return None, len(candidates) > 1


def plan_engine_input(
    source: SourceFacts,
    *,
    selected_video_index: int | None = None,
    selected_asr_audio_index: int | None = None,
    policy: EngineInputPolicy = INITIAL_ENGINE_INPUT_POLICY,
) -> SourcePlan:
    """Plan a path into unchanged Whisper/Jasna interfaces.

    The plan never normalizes media itself.  When resolved stream selection is
    not unambiguous from the original file, it requests a future standardized
    working copy instead of asking the existing engines to accept new options.
    """

    videos = source.video_candidates
    audios = source.audio_streams
    video, video_ambiguous = _resolve_selected_stream(
        videos,
        selected_video_index,
        label="video",
        allow_unique_default=False,
    )
    audio, audio_ambiguous = _resolve_selected_stream(
        audios,
        selected_asr_audio_index,
        label="ASR audio",
        allow_unique_default=True,
    )

    reasons: list[str] = []
    unsupported = False
    if not videos:
        reasons.append("no_video_stream")
        unsupported = True
    if not audios:
        reasons.append("no_audio_stream_for_whisper")
        unsupported = True
    if video is not None and not video.codec_name:
        reasons.append("selected_video_codec_unknown")
        unsupported = True
    if audio is not None and not audio.codec_name:
        reasons.append("selected_asr_audio_codec_unknown")
        unsupported = True

    if video_ambiguous:
        reasons.append("multiple_video_streams_require_selection")
    if audio_ambiguous:
        reasons.append("multiple_audio_streams_require_selection")

    standardization_reasons: list[str] = []
    if source.attached_pictures:
        standardization_reasons.append("attached_picture_requires_standardization")
    if len(videos) > 1 and video is not None:
        standardization_reasons.append("multiple_video_streams_require_standardization")
    if len(audios) > 1 and audio is not None:
        standardization_reasons.append("multiple_audio_streams_require_standardization")
    if not set(source.format_names).intersection(policy.format_names):
        standardization_reasons.append("container_requires_standardization")
    if (
        video is not None
        and video.codec_name
        and video.codec_name not in policy.video_codecs
    ):
        standardization_reasons.append("video_codec_requires_standardization")
    if (
        audio is not None
        and audio.codec_name
        and audio.codec_name not in policy.asr_audio_codecs
    ):
        standardization_reasons.append("asr_audio_codec_requires_standardization")

    if unsupported:
        status = SourcePlanStatus.UNSUPPORTED
    elif video_ambiguous or audio_ambiguous:
        status = SourcePlanStatus.NEEDS_SELECTION
    elif standardization_reasons:
        status = SourcePlanStatus.NEEDS_STANDARDIZATION
        reasons.extend(standardization_reasons)
    else:
        status = SourcePlanStatus.PASS_THROUGH

    return SourcePlan(
        status=status,
        selected_video_index=video.index if video is not None else None,
        selected_asr_audio_index=audio.index if audio is not None else None,
        preserved_audio_indices=tuple(stream.index for stream in audios),
        format_names=source.format_names,
        reasons=tuple(reasons),
    )


class FFprobeSourceProbe:
    """Small injectable FFprobe boundary; tests can supply a fake runner."""

    def __init__(
        self,
        ffprobe: str | Path,
        *,
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.ffprobe = Path(ffprobe)
        self.process_runner = process_runner

    def probe_payload(self, source: str | Path) -> Mapping[str, Any]:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        args = [
            str(self.ffprobe),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(source_path),
        ]
        result = self.process_runner(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise SourceProbeError(
                f"FFprobe failed with exit code {result.returncode}: {detail}"
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SourceProbeError("FFprobe returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise SourceProbeError("FFprobe payload must be an object")
        return payload

    def probe(self, source: str | Path) -> SourceFacts:
        return parse_ffprobe_payload(self.probe_payload(source))
