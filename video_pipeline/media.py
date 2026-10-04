from __future__ import annotations

import json
import locale
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


ProgressCallback = Callable[[float, str], None]


def run_logged_process(
    args: list[str],
    *,
    log_path: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    progress: ProgressCallback | None = None,
    duration_seconds: float | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("\n[command]\n")
        log.write(json.dumps(args, ensure_ascii=False) + "\n")
        log.flush()
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        last_percent = -1.0
        try:
            with process.stdout:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    if not progress:
                        continue
                    percent: float | None = None
                    if duration_seconds:
                        match = re.search(r"out_time_us=(\d+)", line)
                        if match:
                            percent = min(
                                99.0,
                                int(match.group(1))
                                / 1_000_000
                                / duration_seconds
                                * 100,
                            )
                    if percent is None:
                        match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%", line)
                        if match:
                            percent = min(99.0, float(match.group(1)))
                    if percent is not None and percent >= last_percent + 2:
                        last_percent = percent
                        progress(percent, line.strip()[-240:])
            return_code = process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            raise
        if return_code != 0:
            raise RuntimeError(
                f"外部工具退出码 {return_code}；详情见日志 {log_path}"
            )


def probe_media(ffprobe: Path, path: Path, *, count_frames: bool = False) -> dict[str, Any]:
    args = [
        str(ffprobe),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
    ]
    if count_frames:
        args.extend(["-count_frames"])
    args.append(str(path))
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
        raise RuntimeError(f"FFprobe 无法读取媒体 {path}: {result.stderr.strip()[-500:]}")
    return json.loads(result.stdout)


def first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    raise RuntimeError(f"媒体缺少 {codec_type} 流")


def streams_of_type(probe: dict[str, Any], codec_type: str) -> list[dict[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == codec_type
    ]


def parse_fraction(value: str | None) -> Fraction:
    if not value or value in {"0/0", "N/A"}:
        raise RuntimeError(f"无效帧率: {value}")
    result = Fraction(value)
    if result <= 0:
        raise RuntimeError(f"无效帧率: {value}")
    return result


def duration_of(probe: dict[str, Any]) -> float:
    value = probe.get("format", {}).get("duration")
    if value in (None, "N/A"):
        for stream in probe.get("streams", []):
            if stream.get("duration") not in (None, "N/A"):
                return float(stream["duration"])
        raise RuntimeError("媒体没有可用时长")
    return float(value)


def decoded_frame_count(stream: dict[str, Any]) -> int:
    for key in ("nb_read_frames", "nb_frames"):
        value = stream.get(key)
        if value not in (None, "N/A"):
            return int(value)
    raise RuntimeError("无法取得解码帧数")
