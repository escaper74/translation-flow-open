from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .srt import parse_srt


Emit = Callable[..., Any] | Any
ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
SOFT_DURATION_WARNING_SECONDS = 0.5
HARD_DURATION_FAILURE_SECONDS = 2.0


def validate_completed_mkv(path: Path, report_path: Path, source: Path, chinese: Path) -> None:
    """Reuse an existing original-picture package without starting FFmpeg."""
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict):
        raise ValueError("MKV 技术报告必须是 JSON object")
    checks = report.get("checks", {})
    stat = path.stat()
    if (report.get("status") != "passed" or not isinstance(checks, dict) or not checks
            or any(value is not True for value in checks.values())
            or Path(report.get("source", "")).resolve() != source.resolve()
            or Path(report.get("chinese_srt", "")).resolve() != chinese.resolve()
            or Path(report.get("output", "")).resolve() != path.resolve()
            or report.get("output_file") != {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}):
        raise ValueError("MKV 检查点与通过的技术报告不匹配；保留现场并停止")


def _emit(target: Emit | None, event: str, **payload: Any) -> None:
    if target is None:
        return
    method = getattr(target, "emit", None)
    if callable(method):
        method(event, **payload)
    elif callable(target):
        target(event, **payload)


def _streams(probe: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == kind
    ]


def _duration(probe: Mapping[str, Any]) -> float:
    value = probe.get("format", {}).get("duration")
    if value not in (None, "N/A"):
        return float(value)
    for stream in probe.get("streams", []):
        value = stream.get("duration")
        if value not in (None, "N/A"):
            return float(value)
    raise ValueError("media probe has no usable duration")


def _publish_exclusive(candidate: Path, destination: Path) -> None:
    """Publish a same-volume candidate without replacing on the Windows host."""

    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    os.rename(candidate, destination)


class MkvSoftSubtitlePackager:
    """Copy the primary video and every audio stream, adding one zho SRT track."""

    def __init__(
        self,
        ffmpeg: str | Path,
        ffprobe: str | Path,
        *,
        process_runner: ProcessRunner = subprocess.run,
        subtitle_title: str = "Simplified Chinese",
    ) -> None:
        self.ffmpeg = Path(ffmpeg)
        self.ffprobe = Path(ffprobe)
        self.process_runner = process_runner
        self.subtitle_title = subtitle_title

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
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise RuntimeError(f"{label} failed with exit code {result.returncode}: {detail}")
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
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"FFprobe returned invalid JSON for {path}") from exc

    def _short_decode(self, candidate: Path, duration: float, position: float) -> None:
        length = min(1.0, duration)
        self._run(
            [
                str(self.ffmpeg),
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(candidate),
                "-t",
                f"{length:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-f",
                "null",
                "NUL" if os.name == "nt" else "/dev/null",
            ],
            f"short decode at {position:.3f}s",
        )

    @staticmethod
    def _validate(
        source_probe: Mapping[str, Any], output_probe: Mapping[str, Any]
    ) -> dict[str, Any]:
        source_video = _streams(source_probe, "video")
        output_video = _streams(output_probe, "video")
        source_audio = _streams(source_probe, "audio")
        output_audio = _streams(output_probe, "audio")
        output_subtitles = _streams(output_probe, "subtitle")
        output_formats = {
            item.strip().casefold()
            for item in str(output_probe.get("format", {}).get("format_name", "")).split(",")
            if item.strip()
        }
        checks = {
            "matroska_container": "matroska" in output_formats,
            "one_primary_video": len(source_video) >= 1 and len(output_video) == 1,
            "video_codec_copied": bool(source_video)
            and bool(output_video)
            and source_video[0].get("codec_name") == output_video[0].get("codec_name"),
            "video_dimensions_preserved": bool(source_video)
            and bool(output_video)
            and (source_video[0].get("width"), source_video[0].get("height"))
            == (output_video[0].get("width"), output_video[0].get("height")),
            "all_audio_streams_copied": len(source_audio) == len(output_audio),
            "audio_codecs_copied": [item.get("codec_name") for item in source_audio]
            == [item.get("codec_name") for item in output_audio],
            "one_chinese_subtitle": len(output_subtitles) == 1,
            "subtitle_codec_subrip": len(output_subtitles) == 1
            and output_subtitles[0].get("codec_name") in {"subrip", "srt"},
            "subtitle_language_zho": len(output_subtitles) == 1
            and output_subtitles[0].get("tags", {}).get("language") == "zho",
            "subtitle_default": len(output_subtitles) == 1
            and output_subtitles[0].get("disposition", {}).get("default") == 1,
        }
        source_duration = _duration(source_probe)
        output_duration = _duration(output_probe)
        duration_difference = abs(source_duration - output_duration)
        checks["duration_preserved"] = (
            duration_difference <= HARD_DURATION_FAILURE_SECONDS
        )
        failures = [name for name, ok in checks.items() if not ok]
        if failures:
            raise ValueError("MKV technical validation failed: " + ", ".join(failures))
        warnings = []
        if duration_difference > SOFT_DURATION_WARNING_SECONDS:
            warnings.append(
                "container duration differs from the source by "
                f"{duration_difference:.3f}s; accepted as a playable container-level offset"
            )
        return {
            "checks": checks,
            "source_duration_seconds": source_duration,
            "output_duration_seconds": output_duration,
            "duration_difference_seconds": duration_difference,
            "warnings": warnings,
            "source_audio_streams": len(source_audio),
            "output_audio_streams": len(output_audio),
            "output_subtitle_streams": len(output_subtitles),
        }

    def package(
        self,
        source: str | Path,
        chinese_srt: str | Path,
        output: str | Path,
        report_path: str | Path,
        emit: Emit | None = None,
    ) -> Path:
        source, chinese_srt, output, report_path = map(
            Path, (source, chinese_srt, output, report_path)
        )
        if not source.is_file():
            raise FileNotFoundError(source)
        if not chinese_srt.is_file():
            raise FileNotFoundError(chinese_srt)
        if not self.ffmpeg.is_file() or not self.ffprobe.is_file():
            raise FileNotFoundError("ffmpeg/ffprobe tool path is missing")
        if output.suffix.casefold() != ".mkv":
            raise ValueError("soft-subtitle output must use the .mkv extension")
        if output.resolve() == source.resolve():
            raise ValueError("output must not overwrite the source media")
        existing = [str(path) for path in (output, report_path) if path.exists()]
        if existing:
            raise FileExistsError("refusing to overwrite existing output: " + ", ".join(existing))
        subtitle_cues = parse_srt(chinese_srt)

        output.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        candidate = output.parent / f".{output.name}.{uuid.uuid4().hex}.candidate.mkv"
        report_candidate = report_path.parent / (
            f".{report_path.name}.{uuid.uuid4().hex}.candidate.json"
        )
        output_published = False
        _emit(emit, "stage_started", stage="mkv_packaging", overall_percent=83)
        try:
            self._run(
                [
                    str(self.ffmpeg),
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(source),
                    "-f",
                    "srt",
                    "-i",
                    str(chinese_srt),
                    "-map",
                    "0:V:0",
                    "-map",
                    "0:a?",
                    "-map",
                    "1:0",
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-c:s",
                    "srt",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-metadata:s:s:0",
                    f"title={self.subtitle_title}",
                    "-disposition:s:0",
                    "default",
                    str(candidate),
                ],
                "MKV mux",
            )
            if not candidate.is_file():
                raise RuntimeError("FFmpeg reported success but produced no MKV candidate")
            _emit(emit, "stage_completed", stage="mkv_packaging", overall_percent=88)
            _emit(emit, "stage_started", stage="technical_validation", overall_percent=89)
            source_probe = self._probe(source)
            candidate_probe = self._probe(candidate)
            technical = self._validate(source_probe, candidate_probe)
            for warning in technical.get("warnings", []):
                _emit(
                    emit,
                    "warning",
                    stage="technical_validation",
                    code="CONTAINER_DURATION_WARNING",
                    message=str(warning),
                )
            duration = technical["output_duration_seconds"]
            positions = (0.0, max(0.0, duration / 2 - 0.5), max(0.0, duration - 1.0))
            for position in positions:
                self._short_decode(candidate, duration, position)
            technical["short_decode_positions_seconds"] = [round(item, 3) for item in positions]
            technical.update(
                {
                    "status": "passed",
                    "source": str(source.resolve()),
                    "chinese_srt": str(chinese_srt.resolve()),
                    "output": str(output.resolve()),
                    "video_audio_copy": True,
                    "subtitle_language": "zho",
                    "input_subtitle_cues": len(subtitle_cues),
                    "output_file": {"size_bytes": candidate.stat().st_size,
                                    "mtime_ns": candidate.stat().st_mtime_ns},
                }
            )
            report_candidate.write_text(
                json.dumps(technical, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _publish_exclusive(candidate, output)
            output_published = True
            try:
                _publish_exclusive(report_candidate, report_path)
            except BaseException:
                output.unlink(missing_ok=True)
                output_published = False
                raise
        except BaseException:
            # A failed media candidate may remain for diagnosis, but never as the final output.
            report_candidate.unlink(missing_ok=True)
            if output_published:
                output.unlink(missing_ok=True)
            raise
        _emit(
            emit,
            "stage_completed",
            stage="technical_validation",
            output=str(output),
            technical_validation="passed",
            overall_percent=99,
        )
        return output
