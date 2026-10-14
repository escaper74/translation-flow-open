from __future__ import annotations

import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .config import PipelineOptions
from .media import (
    decoded_frame_count,
    duration_of,
    first_stream,
    parse_fraction,
    probe_media,
    run_logged_process,
    streams_of_type,
)
from .state import sha256_file, utc_now


def _capture_lines(args: list[str]) -> list[str]:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"验收命令失败（退出码 {result.returncode}）: {result.stderr.strip()[-500:]}"
        )
    return result.stdout.replace("\r", "").splitlines()


def _video_pts(ffprobe: Path, output: Path) -> list[float]:
    lines = _capture_lines(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(output),
        ]
    )
    values: list[float] = []
    for line in lines:
        token = line.strip().split(",", 1)[0]
        if token and token != "N/A":
            values.append(float(token))
    return values


def _packet_bounds(ffprobe: Path, output: Path, selector: str) -> tuple[float, float]:
    lines = _capture_lines(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_packets",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "csv=p=0",
            str(output),
        ]
    )
    starts: list[float] = []
    ends: list[float] = []
    for line in lines:
        parts = [part.strip() for part in line.split(",")]
        if not parts or parts[0] in {"", "N/A"}:
            continue
        start = float(parts[0])
        duration = 0.0
        if len(parts) > 1 and parts[1] not in {"", "N/A"}:
            duration = float(parts[1])
        starts.append(start)
        ends.append(start + duration)
    if not starts:
        raise RuntimeError(f"验收没有读到 {selector} 数据包")
    return min(starts), max(ends)


def _frame_md5(ffmpeg: Path, output: Path) -> tuple[int, int]:
    lines = _capture_lines(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(output),
            "-map",
            "0:v:0",
            "-f",
            "framemd5",
            "-",
        ]
    )
    hashes: list[str] = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 6:
            hashes.append(parts[-1])
    duplicates = sum(left == right for left, right in zip(hashes, hashes[1:]))
    return len(hashes), duplicates


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def validate_output(
    *,
    ffmpeg: Path,
    ffprobe: Path,
    source: Path,
    output: Path,
    options: PipelineOptions,
    input_fingerprint: dict[str, Any],
    request: dict[str, Any],
    log_path: Path,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failures.append(f"{name}: {detail}")

    if progress:
        progress(5, "读取输入和输出规格")
    source_probe = probe_media(ffprobe, source, count_frames=True)
    output_probe = probe_media(ffprobe, output, count_frames=True)
    source_video = first_stream(source_probe, "video")
    output_video = first_stream(output_probe, "video")
    source_audio = streams_of_type(source_probe, "audio")
    output_audio = streams_of_type(output_probe, "audio")
    output_subtitles = streams_of_type(output_probe, "subtitle")

    source_width = int(source_video["width"])
    source_height = int(source_video["height"])
    output_width = int(output_video["width"])
    output_height = int(output_video["height"])
    check(
        "resolution_preserved",
        (source_width, source_height) == (output_width, output_height),
        f"source={source_width}x{source_height} output={output_width}x{output_height}",
    )
    check(
        "no_4k_upscale",
        output_width <= source_width and output_height <= source_height,
        f"source={source_width}x{source_height} output={output_width}x{output_height}",
    )

    # CFR 流的 r_frame_rate 保留目标有理数；极短片的 avg_frame_rate 会因
    # 容器尾部取整变成等价近似分数。真实节奏另由逐帧 PTS 和帧数验证。
    source_rate = parse_fraction(
        source_video.get("r_frame_rate") or source_video.get("avg_frame_rate")
    )
    output_rate = parse_fraction(
        output_video.get("r_frame_rate") or output_video.get("avg_frame_rate")
    )
    expected_rate = source_rate * (
        2 if options.enable_frame_interpolation_2x else 1
    )
    check(
        "frame_rate",
        abs(float(output_rate - expected_rate)) <= 0.001,
        f"expected={_fraction_text(expected_rate)} actual={_fraction_text(output_rate)}",
    )

    source_frames = decoded_frame_count(source_video)
    output_frames = decoded_frame_count(output_video)
    expected_frames = (
        source_frames * 2 - 1
        if options.enable_frame_interpolation_2x
        else source_frames
    )
    check(
        "frame_count",
        abs(output_frames - expected_frames) <= 2,
        f"source={source_frames} expected={expected_frames} output={output_frames}",
    )

    source_duration = duration_of(source_probe)
    output_duration = duration_of(output_probe)
    check(
        "duration",
        abs(output_duration - source_duration) <= 0.5,
        f"source={source_duration:.6f}s output={output_duration:.6f}s tolerance=0.5s",
    )
    check(
        "video_stream_count",
        len(streams_of_type(output_probe, "video")) == 1,
        f"output_video_streams={len(streams_of_type(output_probe, 'video'))}",
    )
    check(
        "audio_stream_count",
        len(output_audio) == len(source_audio),
        f"source={len(source_audio)} output={len(output_audio)}",
    )
    source_audio_codecs = [stream.get("codec_name") for stream in source_audio]
    output_audio_codecs = [stream.get("codec_name") for stream in output_audio]
    check(
        "audio_codecs_copied",
        output_audio_codecs == source_audio_codecs,
        f"source={source_audio_codecs} output={output_audio_codecs}",
    )
    check(
        "subtitles_omitted",
        not output_subtitles,
        f"output_subtitle_streams={len(output_subtitles)}",
    )

    if progress:
        progress(20, "完整解码音视频")
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    run_logged_process(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(output),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-f",
            "null",
            null_target,
        ],
        log_path=log_path,
    )
    check("full_audio_video_decode", True, "FFmpeg 完整解码通过")

    if progress:
        progress(45, "检查显示时间戳")
    pts = _video_pts(ffprobe, output)
    non_monotonic = sum(right <= left for left, right in zip(pts, pts[1:]))
    duplicate_pts = sum(right == left for left, right in zip(pts, pts[1:]))
    check(
        "video_pts_strictly_monotonic",
        bool(pts) and non_monotonic == 0,
        f"frames={len(pts)} non_monotonic={non_monotonic} duplicate_pts={duplicate_pts}",
    )

    video_start, video_end = _packet_bounds(ffprobe, output, "v:0")
    if output_audio:
        audio_start, audio_end = _packet_bounds(ffprobe, output, "a:0")
        start_delta = abs(video_start - audio_start)
        tail_delta = abs(video_end - audio_end)
        check(
            "audio_video_start_delta",
            start_delta <= 0.1,
            f"video={video_start:.6f}s audio={audio_start:.6f}s delta={start_delta:.6f}s",
        )
        check(
            "audio_video_tail_delta",
            tail_delta <= 0.5,
            f"video={video_end:.6f}s audio={audio_end:.6f}s delta={tail_delta:.6f}s",
        )
    else:
        audio_start = audio_end = None
        check("audio_video_start_delta", not source_audio, "源和输出均无音频")
        check("audio_video_tail_delta", not source_audio, "源和输出均无音频")

    if progress:
        progress(65, "检查相邻完全重复帧")
    md5_frame_count, adjacent_duplicates = _frame_md5(ffmpeg, output)
    duplicate_ok = (
        adjacent_duplicates == 0
        if options.enable_frame_interpolation_2x
        else True
    )
    check(
        "adjacent_exact_duplicate_frames",
        duplicate_ok,
        f"decoded_frames={md5_frame_count} adjacent_duplicates={adjacent_duplicates}",
    )

    if progress:
        progress(90, "计算输出 SHA-256")
    output_hash = sha256_file(output)
    report = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "status": "passed" if not failures else "failed",
        "input_fingerprint": input_fingerprint,
        "request": request,
        "output": {
            "path": str(output.resolve()),
            "bytes": output.stat().st_size,
            "sha256": output_hash,
        },
        "spec": {
            "source_resolution": f"{source_width}x{source_height}",
            "output_resolution": f"{output_width}x{output_height}",
            "source_frame_rate": _fraction_text(source_rate),
            "expected_frame_rate": _fraction_text(expected_rate),
            "output_frame_rate": _fraction_text(output_rate),
            "source_frames": source_frames,
            "expected_output_frames": expected_frames,
            "output_frames": output_frames,
            "source_duration_seconds": source_duration,
            "output_duration_seconds": output_duration,
            "video_start_seconds": video_start,
            "video_end_seconds": video_end,
            "audio_start_seconds": audio_start,
            "audio_end_seconds": audio_end,
            "adjacent_exact_duplicate_frames": adjacent_duplicates,
        },
        "checks": checks,
        "failures": failures,
        "limitations": [
            "自动技术检查不评价去马赛克或增强后的视觉质量。",
            "帧率、时间戳和重复帧检查不能证明完全没有插帧重影。",
            "最终视觉验收仍需用户用真实播放器人工观看。",
        ],
    }
    if progress:
        progress(100, report["status"])
    return report


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
