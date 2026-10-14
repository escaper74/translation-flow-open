from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import subprocess
import uuid
from typing import Any, Callable, Mapping, Sequence

from translation_flow.srt import (
    SubtitleCue,
    parse_srt,
    validate_positive_durations,
    write_srt,
)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
SOFT_DURATION_WARNING_SECONDS = Decimal("0.75")
HARD_DURATION_FAILURE_SECONDS = Decimal("2.0")


class FinalMuxError(RuntimeError):
    """Raised when the final mux cannot safely produce a formal output."""


@dataclass(frozen=True, slots=True)
class FinalMuxConfig:
    processed_video: Path
    source_media: Path
    chinese_srt: Path | None
    source_start_seconds: str
    output: Path
    work_dir: Path
    seam_positions_seconds: tuple[str, ...] = ()
    preserve_source_subtitles: bool = False

    @property
    def shifted_srt(self) -> Path:
        return self.work_dir / "shifted.zh.srt"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "final-mux.manifest.json"

    @property
    def report_path(self) -> Path:
        return self.work_dir / "final-mux.report.json"


def _milliseconds(value: Decimal) -> int:
    return int((value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_seconds(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise FinalMuxError(f"{label} must be a decimal number: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise FinalMuxError(f"{label} must be finite and non-negative: {value!r}")
    return parsed


def _duration(probe: Mapping[str, Any], label: str) -> Decimal:
    value = probe.get("format", {}).get("duration")
    if value not in (None, "N/A"):
        return _decimal_seconds(str(value), f"{label} duration")
    for stream in probe.get("streams", []):
        value = stream.get("duration")
        if value not in (None, "N/A"):
            return _decimal_seconds(str(value), f"{label} duration")
    raise FinalMuxError(f"{label} probe has no usable duration")


def _streams(probe: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == kind
    ]


def _json_write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_exclusive(candidate: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    os.rename(candidate, destination)


def shift_cues_to_window(
    cues: Sequence[SubtitleCue], start_ms: int, duration_ms: int
) -> list[SubtitleCue]:
    """Clip cues to a source window and shift the surviving timeline to zero."""

    if start_ms < 0 or duration_ms <= 0:
        raise ValueError("subtitle window must have a non-negative start and positive duration")
    validate_positive_durations(cues, label="final mux subtitle input")
    end_ms = start_ms + duration_ms
    shifted: list[SubtitleCue] = []
    for cue in cues:
        if cue.end_ms <= start_ms or cue.start_ms >= end_ms:
            continue
        clipped_start = max(cue.start_ms, start_ms)
        clipped_end = min(cue.end_ms, end_ms)
        if clipped_end <= clipped_start:
            continue
        shifted.append(
            SubtitleCue(
                index=len(shifted) + 1,
                start_ms=clipped_start - start_ms,
                end_ms=clipped_end - start_ms,
                text=cue.text,
            )
        )
    return shifted


class FinalMuxWorkflow:
    """Inspect, mux and technically validate one accepted picture/subtitle window."""

    def __init__(
        self,
        ffmpeg: str | Path,
        ffprobe: str | Path,
        *,
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.ffmpeg = Path(ffmpeg)
        self.ffprobe = Path(ffprobe)
        self.process_runner = process_runner

    def _run(self, args: list[str], label: str) -> subprocess.CompletedProcess[str]:
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
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise FinalMuxError(
                f"{label} failed with exit code {result.returncode}: {detail}"
            )
        return result

    def _probe(self, path: Path) -> dict[str, Any]:
        result = self._run(
            [
                str(self.ffprobe),
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            f"FFprobe {path.name}",
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FinalMuxError(f"FFprobe returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise FinalMuxError(f"FFprobe returned an invalid payload for {path}")
        return payload

    def _validate_static(self, config: FinalMuxConfig, *, for_run: bool) -> None:
        required_files = [
            ("processed video", config.processed_video),
            ("source media", config.source_media),
            ("FFmpeg", self.ffmpeg),
            ("FFprobe", self.ffprobe),
        ]
        if config.chinese_srt is not None:
            required_files.append(("Chinese SRT", config.chinese_srt))
        for label, path in required_files:
            if not path.is_file():
                raise FileNotFoundError(f"{label} does not exist: {path}")
        if config.processed_video.suffix.casefold() != ".mkv":
            raise FinalMuxError("processed video must be an MKV")
        if config.output.suffix.casefold() not in {".mkv", ".mp4"}:
            raise FinalMuxError("final output must use the .mkv or .mp4 extension")
        if config.preserve_source_subtitles and config.chinese_srt is not None:
            raise FinalMuxError("choose source subtitles or translated SRT")
        resolved_inputs = {
            config.processed_video.resolve(),
            config.source_media.resolve(),
        }
        if config.chinese_srt is not None:
            resolved_inputs.add(config.chinese_srt.resolve())
        if config.output.resolve() in resolved_inputs:
            raise FinalMuxError("final output must not overwrite any input")
        if config.work_dir.resolve() == config.output.resolve():
            raise FinalMuxError("work-dir and final output must be different paths")
        if config.output.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {config.output}")
        if config.work_dir.exists() and not config.work_dir.is_dir():
            raise FinalMuxError(f"work-dir is not a directory: {config.work_dir}")
        if for_run:
            retry_failed = False
            if config.manifest_path.is_file():
                prior = json.loads(config.manifest_path.read_text(encoding="utf-8"))
                retry_failed = (prior.get("status") == "failed"
                                and prior.get("output") == str(config.output.resolve())
                                and prior.get("input_lineage") == {
                                    "processed_video": str(config.processed_video.resolve()),
                                    "source_media": str(config.source_media.resolve()),
                                    "full_timeline_chinese_srt": str(config.chinese_srt.resolve()) if config.chinese_srt else None})
            existing = [
                str(path)
                for path in (
                    config.shifted_srt,
                    config.manifest_path,
                    config.report_path,
                )
                if path.exists() and not (retry_failed and path in (config.manifest_path, config.shifted_srt))
            ]
            if existing:
                raise FileExistsError(
                    "refusing to overwrite existing workflow artifacts: "
                    + ", ".join(existing)
                )

    @staticmethod
    def _validate_inputs(
        processed_probe: Mapping[str, Any],
        source_probe: Mapping[str, Any],
        source_start: Decimal,
        processed_duration: Decimal,
        *, allow_silent: bool = False,
    ) -> None:
        processed_video = _streams(processed_probe, "video")
        processed_audio = _streams(processed_probe, "audio")
        processed_subtitles = _streams(processed_probe, "subtitle")
        processed_other = [
            stream
            for stream in processed_probe.get("streams", [])
            if stream.get("codec_type") != "video"
        ]
        source_audio = _streams(source_probe, "audio")
        failures: list[str] = []
        if len(processed_video) != 1:
            failures.append("processed_video_count")
        if processed_audio:
            failures.append("processed_has_audio")
        if processed_subtitles:
            failures.append("processed_has_subtitles")
        if processed_other and not (processed_audio or processed_subtitles):
            failures.append("processed_has_non_video_streams")
        if not source_audio and not allow_silent:
            failures.append("source_has_no_audio")
        if processed_duration <= 0:
            failures.append("processed_duration")
        source_duration = _duration(source_probe, "source media")
        if (
            source_start + processed_duration
            > source_duration + HARD_DURATION_FAILURE_SECONDS
        ):
            failures.append("source_window_out_of_range")
        if failures:
            raise FinalMuxError("final mux input validation failed: " + ", ".join(failures))

    @staticmethod
    def _validate_output(
        processed_probe: Mapping[str, Any],
        source_probe: Mapping[str, Any],
        output_probe: Mapping[str, Any],
        *,
        expect_subtitle: bool = True,
        preserve_source_subtitles: bool = False,
        output_target: str = "mkv",
    ) -> dict[str, Any]:
        processed_video = _streams(processed_probe, "video")
        output_video = _streams(output_probe, "video")
        source_audio = _streams(source_probe, "audio")
        output_audio = _streams(output_probe, "audio")
        output_subtitles = _streams(output_probe, "subtitle")
        formats = {
            item.strip().casefold()
            for item in str(output_probe.get("format", {}).get("format_name", "")).split(",")
            if item.strip()
        }
        processed_duration = _duration(processed_probe, "processed video")
        output_duration = _duration(output_probe, "final output")
        duration_difference = abs(output_duration - processed_duration)
        checks: dict[str, bool] = {
            "matroska_container" if output_target == "mkv" else "mp4_container":
                ("matroska" in formats if output_target == "mkv" else "mp4" in formats),
            "exactly_one_video": len(output_video) == 1,
            "video_codec_preserved": len(processed_video) == 1
            and len(output_video) == 1
            and processed_video[0].get("codec_name") == output_video[0].get("codec_name"),
            "video_dimensions_preserved": len(processed_video) == 1
            and len(output_video) == 1
            and (
                processed_video[0].get("width"),
                processed_video[0].get("height"),
            )
            == (output_video[0].get("width"), output_video[0].get("height")),
            "video_default": len(output_video) == 1
            and output_video[0].get("disposition", {}).get("default") == 1,
            "all_source_audio_streams_present": len(source_audio) == len(output_audio),
            "audio_codecs_preserved": [item.get("codec_name") for item in source_audio]
            == [item.get("codec_name") for item in output_audio],
            "duration_close_to_processed": (
                duration_difference <= HARD_DURATION_FAILURE_SECONDS
            ),
        }
        if preserve_source_subtitles:
            source_subtitles = _streams(source_probe, "subtitle")
            checks["all_source_subtitles_present"] = len(source_subtitles) == len(output_subtitles)
            checks["source_subtitle_codecs_preserved_or_converted"] = [
                item.get("codec_name") for item in output_subtitles
            ] == ["mov_text" if output_target == "mp4" else
                  ("subrip" if item.get("codec_name") == "mov_text" else item.get("codec_name"))
                  for item in source_subtitles]
            checks["source_subtitle_languages_preserved"] = [
                item.get("tags", {}).get("language", "und") for item in output_subtitles
            ] == [item.get("tags", {}).get("language", "und") for item in source_subtitles]
        elif expect_subtitle:
            checks.update(
                {
                    "exactly_one_subtitle": len(output_subtitles) == 1,
                    "subtitle_codec_subrip" if output_target == "mkv" else "subtitle_codec_mov_text": len(output_subtitles) == 1
                    and output_subtitles[0].get("codec_name") in ({"subrip", "srt"} if output_target == "mkv" else {"mov_text"}),
                    "subtitle_language_zho": len(output_subtitles) == 1
                    and output_subtitles[0].get("tags", {}).get("language") == "zho",
                    "subtitle_default": len(output_subtitles) == 1
                    and output_subtitles[0].get("disposition", {}).get("default") == 1,
                }
            )
        else:
            checks["no_subtitle_streams"] = not output_subtitles
        failures = [name for name, passed in checks.items() if not passed]
        if failures:
            raise FinalMuxError(
                "final MKV technical validation failed: " + ", ".join(failures)
            )
        warnings = []
        if duration_difference > SOFT_DURATION_WARNING_SECONDS:
            warnings.append(
                "container duration differs from the processed video by "
                f"{duration_difference}s; accepted as a playable container-level offset"
            )
        return {
            "checks": checks,
            "processed_duration_seconds": float(processed_duration),
            "output_duration_seconds": float(output_duration),
            "duration_difference_seconds": float(duration_difference),
            "warnings": warnings,
            "source_audio_streams": len(source_audio),
            "output_audio_streams": len(output_audio),
        }

    def _prepare(self, config: FinalMuxConfig, *, for_run: bool) -> dict[str, Any]:
        self._validate_static(config, for_run=for_run)
        source_start = _decimal_seconds(
            config.source_start_seconds, "source-start-seconds"
        )
        processed_probe = self._probe(config.processed_video)
        source_probe = self._probe(config.source_media)
        processed_duration = _duration(processed_probe, "processed video")
        self._validate_inputs(
            processed_probe, source_probe, source_start, processed_duration,
            allow_silent=config.preserve_source_subtitles,
        )
        if config.preserve_source_subtitles and config.output.suffix.lower() == ".mp4":
            if any(s.get("codec_name") not in {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
                   for s in _streams(source_probe, "subtitle")):
                raise FinalMuxError("MP4 无法保留此视频的图形字幕，请选择 MKV。")
        source_start_ms = _milliseconds(source_start)
        duration_ms = _milliseconds(processed_duration)
        cues: list[SubtitleCue] = []
        shifted: list[SubtitleCue] = []
        selected_source_indices: list[int] = []
        if config.chinese_srt is not None:
            try:
                cues = parse_srt(config.chinese_srt)
                shifted = shift_cues_to_window(cues, source_start_ms, duration_ms)
            except ValueError as exc:
                raise FinalMuxError(f"invalid Chinese subtitle input: {exc}") from exc
            if not shifted:
                raise FinalMuxError(
                    "no Chinese subtitle cues intersect the processed video window"
                )
            selected_source_indices = [
                cue.index
                for cue in cues
                if min(cue.end_ms, source_start_ms + duration_ms)
                > max(cue.start_ms, source_start_ms)
            ]
            if len(shifted) != len(selected_source_indices):
                raise FinalMuxError(
                    "subtitle window selection changed cue count unexpectedly"
                )

        seams: list[Decimal] = []
        for raw in config.seam_positions_seconds:
            seam = _decimal_seconds(raw, "seam-position-seconds")
            if seam <= 0 or seam >= processed_duration:
                raise FinalMuxError(
                    "seam-position-seconds must be inside the processed output window: "
                    + raw
                )
            if seam not in seams:
                seams.append(seam)

        return {
            "schema_version": 1,
            "status": "planned",
            "input_lineage": {
                "processed_video": str(config.processed_video.resolve()),
                "source_media": str(config.source_media.resolve()),
                "full_timeline_chinese_srt": (
                    str(config.chinese_srt.resolve())
                    if config.chinese_srt is not None
                    else None
                ),
            },
            "source_start_seconds": str(source_start),
            "source_start_ms": source_start_ms,
            "processed_duration_seconds": str(processed_duration),
            "processed_duration_ms": duration_ms,
            "source_audio_streams": len(_streams(source_probe, "audio")),
            "subtitle_mode": ("source" if config.preserve_source_subtitles else
                              "soft_zho_srt" if config.chinese_srt is not None else "none"),
            "output_target": config.output.suffix.lower().lstrip("."),
            "selected_subtitle_cues": len(shifted),
            "selected_source_cue_indices": selected_source_indices,
            "subtitle_window": (
                {
                    "first_source_cue_index": selected_source_indices[0],
                    "last_source_cue_index": selected_source_indices[-1],
                    "first_shifted_start_ms": shifted[0].start_ms,
                    "first_shifted_end_ms": shifted[0].end_ms,
                    "last_shifted_start_ms": shifted[-1].start_ms,
                    "last_shifted_end_ms": shifted[-1].end_ms,
                }
                if shifted
                else None
            ),
            "shifted_srt": (
                str(config.shifted_srt.resolve())
                if config.chinese_srt is not None
                else None
            ),
            "output": str(config.output.resolve()),
            "work_dir": str(config.work_dir.resolve()),
            "report": str(config.report_path.resolve()),
            "seam_positions_seconds": [float(item) for item in seams],
            "release_status": "candidate_for_human_review",
            "human_review_status": "pending",
            "human_acceptance": "pending",
            "_processed_probe": processed_probe,
            "_source_probe": source_probe,
            "_shifted_cues": shifted,
            "_source_start_decimal": source_start,
            "_processed_duration_decimal": processed_duration,
            "_seams_decimal": seams,
        }

    def build_mux_command(
        self,
        config: FinalMuxConfig,
        duration_seconds: Decimal,
        candidate: Path,
    ) -> list[str]:
        command = [
            str(self.ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(config.processed_video),
            "-ss",
            config.source_start_seconds,
            "-t",
            str(duration_seconds),
            "-i",
            str(config.source_media),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-disposition:v:0",
            "default",
        ]
        if config.chinese_srt is not None:
            source_input = command.index("-map")
            command[source_input:source_input] = [
                "-f",
                "srt",
                "-i",
                str(config.shifted_srt),
            ]
            command.extend(
                [
                    "-map",
                    "2:0",
                    "-c:s",
                    "mov_text" if config.output.suffix.lower() == ".mp4" else "srt",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-metadata:s:s:0",
                    "title=Simplified Chinese",
                    "-disposition:s:0",
                    "default",
                ]
            )
        elif config.preserve_source_subtitles:
            command.extend(["-map", "1:s?", "-c:s", "mov_text" if config.output.suffix.lower() == ".mp4" else "copy"])
            source_probe = self._probe(config.source_media)
            for kind, spec in (("audio", "a"), ("subtitle", "s")):
                for index, stream in enumerate(_streams(source_probe, kind)):
                    command.extend([f"-map_metadata:s:{spec}:{index}", f"1:s:{spec}:{index}"])
            if config.output.suffix.lower() == ".mkv":
                command.extend(["-map", "1:t?", "-c:t", "copy"])
                for index, stream in enumerate(_streams(source_probe, "subtitle")):
                    if stream.get("codec_name") == "mov_text":
                        command.extend([f"-c:s:{index}", "srt"])
        command.extend(["-t", str(duration_seconds), str(candidate)])
        return command

    def plan(self, config: FinalMuxConfig) -> dict[str, Any]:
        prepared = self._prepare(config, for_run=False)
        public = {key: value for key, value in prepared.items() if not key.startswith("_")}
        public["mux_command"] = self.build_mux_command(
            config,
            prepared["_processed_duration_decimal"],
            Path("<candidate-output.mkv>"),
        )
        public["actions"] = [
            "stream-copy processed video and all source audio into one MKV",
            "FFprobe track/duration checks and short decodes",
            "publish only after technical validation",
        ]
        if config.chinese_srt is not None:
            public["actions"][0:0] = [
                "clip and shift intersecting SRT cues to zero",
                "add exactly one default zho SubRip track",
            ]
        return public

    def _short_decode(self, output: Path, duration: Decimal, position: Decimal) -> None:
        remaining = max(Decimal("0.1"), duration - position)
        length = min(Decimal("1.0"), remaining)
        self._run(
            [
                str(self.ffmpeg),
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-ss",
                str(position),
                "-i",
                str(output),
                "-t",
                str(length),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-f",
                "null",
                "NUL" if os.name == "nt" else "/dev/null",
            ],
            f"short decode at {position}s",
        )

    def run(self, config: FinalMuxConfig) -> dict[str, Any]:
        prepared = self._prepare(config, for_run=True)
        public = {key: value for key, value in prepared.items() if not key.startswith("_")}
        config.work_dir.mkdir(parents=True, exist_ok=True)
        config.output.parent.mkdir(parents=True, exist_ok=True)
        if config.manifest_path.exists():
            config.manifest_path.rename(config.work_dir / f"failed-attempt-{uuid.uuid4().hex}.json")
        _json_write_atomic(config.manifest_path, public)

        candidate = config.output.parent / (
            f".{config.output.name}.{uuid.uuid4().hex}.candidate{config.output.suffix}"
        )
        report_candidate = config.work_dir / (
            f".{config.report_path.name}.{uuid.uuid4().hex}.candidate.json"
        )
        output_published = False
        report_published = False
        try:
            if config.chinese_srt is not None:
                write_srt(config.shifted_srt, prepared["_shifted_cues"])
                if parse_srt(config.shifted_srt) != prepared["_shifted_cues"]:
                    raise FinalMuxError("shifted SRT failed its write/read audit")

            self._run(
                self.build_mux_command(
                    config,
                    prepared["_processed_duration_decimal"],
                    candidate,
                ),
                "final MKV mux",
            )
            if not candidate.is_file():
                raise FinalMuxError("FFmpeg reported success but produced no candidate MKV")

            output_probe = self._probe(candidate)
            technical = self._validate_output(
                prepared["_processed_probe"],
                prepared["_source_probe"],
                output_probe,
                expect_subtitle=config.chinese_srt is not None,
                preserve_source_subtitles=config.preserve_source_subtitles,
                output_target=config.output.suffix.lower().lstrip("."),
            )
            duration = prepared["_processed_duration_decimal"]
            positions = [Decimal("0"), *prepared["_seams_decimal"]]
            end_position = max(Decimal("0"), duration - Decimal("1"))
            if end_position not in positions:
                positions.append(end_position)
            for position in positions:
                self._short_decode(candidate, duration, position)
            technical["short_decode_positions_seconds"] = [
                float(position) for position in positions
            ]

            report = {
                **public,
                "status": "technical_checks_passed",
                "release_status": "candidate_for_human_review",
                "human_review_status": "pending",
                "human_acceptance": "pending",
                "technical_validation": technical,
                "video_audio_stream_copy": True,
                "subtitle_text_modified": False,
            }
            report_candidate.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            public["status"] = "technical_checks_passed"
            _json_write_atomic(config.manifest_path, public)

            _publish_exclusive(candidate, config.output)
            output_published = True
            _publish_exclusive(report_candidate, config.report_path)
            report_published = True
            return report
        except BaseException as exc:
            candidate.unlink(missing_ok=True)
            report_candidate.unlink(missing_ok=True)
            if output_published:
                config.output.unlink(missing_ok=True)
            if report_published:
                config.report_path.unlink(missing_ok=True)
            failed = dict(public)
            failed.update(
                {
                    "status": "failed",
                    "release_status": "not_published",
                    "human_review_status": "not_reached",
                    "human_acceptance": "not_reached",
                    "error": str(exc),
                }
            )
            _json_write_atomic(config.manifest_path, failed)
            raise
