from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
import time
from typing import Any, Iterable

from .plan import ExperimentPlan, ExperimentStep


SOFTWARE_DECODE_FALLBACK = "using ffmpeg software decoding and uploading frames to cuda"
PROCESSED_FRAME_PATTERN = re.compile(r"Processed:.*?\((\d+)f\)", re.IGNORECASE)
SHORT_PROCESS_TIMEOUT_SECONDS = 180.0
GPU_STEPS = frozenset({"process-short", "process-long-a", "process-long-b"})
LONG_STEPS = frozenset(
    {"prepare-long", "process-long-a", "process-long-b", "concat-long", "verify-long"}
)
ALL_STEPS = (
    "prepare-short",
    "process-short",
    "verify-short",
    "prepare-long",
    "process-long-a",
    "process-long-b",
    "concat-long",
    "verify-long",
)


class ExperimentError(RuntimeError):
    pass


def has_software_decode_fallback(line: str) -> bool:
    return SOFTWARE_DECODE_FALLBACK in line.casefold()


def _gpu_utilization_percent() -> int | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first_line = result.stdout.strip().splitlines()
    if not first_line:
        return None
    try:
        return int(first_line[0].strip())
    except ValueError:
        return None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.terminate()


def _ensure_ascii_install_path(path: Path) -> None:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ExperimentError(
            f"Jasna 安装路径必须只含英文字母/数字路径字符，当前为：{path}"
        ) from exc


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ExperimentError(f"{label}不存在或不是文件：{path}")


def _require_outputs_absent(paths: Iterable[Path]) -> None:
    existing = [path for path in paths if path.exists()]
    if existing:
        joined = "\n".join(str(path) for path in existing)
        raise ExperimentError(f"为防止覆盖，以下目标已存在：\n{joined}")


def _log_path(plan: ExperimentPlan, step: ExperimentStep) -> Path:
    return plan.paths.logs_dir / f"{step.key}.log"


def _run_streamed(
    command: tuple[str, ...] | list[str],
    log_path: Path,
    *,
    mirror_output: bool = True,
    stop_on_software_decode: bool = False,
    timeout_seconds: float | None = None,
    progress_stall_seconds: float | None = None,
    idle_gpu_threshold_percent: int = 10,
) -> float:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    fallback_detected = False
    timed_out = False
    stalled = False
    stalled_gpu_utilization: int | None = None
    highest_processed_frame = -1
    last_progress_at = started
    last_stall_check_at = 0.0
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write(f"command={subprocess.list2cmdline(command)}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        output: Queue[str | None] = Queue()

        def read_output() -> None:
            try:
                for stream_line in process.stdout:
                    output.put(stream_line)
            finally:
                output.put(None)

        reader = Thread(target=read_output, name="jasna-output-reader", daemon=True)
        reader.start()
        while True:
            now = time.perf_counter()
            elapsed = now - started
            if timeout_seconds is not None and elapsed >= timeout_seconds:
                timed_out = True
                _terminate_process_tree(process)
                break
            if (
                progress_stall_seconds is not None
                and now - last_progress_at >= progress_stall_seconds
                and (
                    last_stall_check_at == 0.0
                    or elapsed - last_stall_check_at >= 15.0
                )
            ):
                last_stall_check_at = elapsed
                stalled_gpu_utilization = _gpu_utilization_percent()
                log.write(
                    "progress_stall_check="
                    f"{elapsed:.3f}s highest_frame={highest_processed_frame} "
                    f"gpu_utilization={stalled_gpu_utilization}\n"
                )
                log.flush()
                if (
                    stalled_gpu_utilization is not None
                    and stalled_gpu_utilization <= idle_gpu_threshold_percent
                ):
                    stalled = True
                    _terminate_process_tree(process)
                    break
            try:
                line = output.get(timeout=0.5)
            except Empty:
                continue
            if line is None:
                break
            if mirror_output:
                sys.stdout.write(line)
                sys.stdout.flush()
            log.write(line)
            log.flush()
            frames = PROCESSED_FRAME_PATTERN.findall(line)
            if frames:
                current_frame = max(int(value) for value in frames)
                if current_frame > highest_processed_frame:
                    highest_processed_frame = current_frame
                    last_progress_at = time.perf_counter()
            if stop_on_software_decode and has_software_decode_fallback(line):
                fallback_detected = True
                _terminate_process_tree(process)
                break
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
        reader.join(timeout=2)
        process.stdout.close()
        elapsed = time.perf_counter() - started
        log.write(f"\nreturn_code={return_code}\nelapsed_seconds={elapsed:.3f}\n")

    if fallback_detected:
        raise ExperimentError(
            "检测到 Jasna 回退到 FFmpeg 软件解码并上传 CUDA；已终止。本轮只保留日志，"
            "不得进入 30 分钟实验，应另开排障任务。"
        )
    if timed_out:
        raise ExperimentError(
            f"外部处理墙钟超过 {timeout_seconds:.0f} 秒，已终止并保留日志：{log_path}。"
            "不要自动重试，应先排查输入、工具或资源。"
        )
    if stalled:
        raise ExperimentError(
            f"Jasna 约 {progress_stall_seconds:.0f} 秒没有帧进展，且 GPU 利用率仅 "
            f"{stalled_gpu_utilization}%，已终止并保留日志：{log_path}。"
            "不要自动重试，应先排查解码、引擎或输入。"
        )
    if return_code != 0:
        raise ExperimentError(f"外部命令失败，退出码 {return_code}；日志：{log_path}")
    return elapsed


def _probe(ffprobe: Path, media: Path) -> dict[str, Any]:
    _require_file(ffprobe, "ffprobe")
    _require_file(media, "待检查媒体")
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,duration",
        "-of",
        "json",
        str(media),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise ExperimentError(f"ffprobe 失败：{media}\n{result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExperimentError(f"ffprobe 没有返回有效 JSON：{media}") from exc
    payload["path"] = str(media)
    return payload


def _duration(probe: dict[str, Any]) -> float:
    raw = probe.get("format", {}).get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ExperimentError(f"无法取得有效时长：{probe.get('path')}") from exc
    if value <= 0:
        raise ExperimentError(f"媒体时长不是正数：{probe.get('path')}")
    return value


def _video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    non_video = [stream for stream in streams if stream.get("codec_type") != "video"]
    if len(videos) != 1 or non_video:
        types = [stream.get("codec_type") for stream in streams]
        raise ExperimentError(
            f"要求恰好一个视频轨且没有音频/字幕/数据轨，实际轨道为 {types}：{probe.get('path')}"
        )
    return videos[0]


def _stream_signature(stream: dict[str, Any]) -> tuple[Any, ...]:
    return (
        stream.get("codec_name"),
        stream.get("width"),
        stream.get("height"),
        stream.get("pix_fmt"),
        stream.get("avg_frame_rate"),
    )


def _assert_close_duration(actual: float, expected: float, label: str) -> None:
    tolerance = max(2.0, expected * 0.005)
    if abs(actual - expected) > tolerance:
        raise ExperimentError(
            f"{label}时长偏差明显：实际 {actual:.3f}s，参照 {expected:.3f}s，允许 {tolerance:.3f}s"
        )


def _assert_extracted_total(actual: float, expected: float, label: str) -> None:
    # Stream-copy cuts land on usable keyframes, so this gate intentionally allows
    # a larger tolerance than a processed input/output comparison.
    tolerance = max(10.0, expected * 0.01)
    if abs(actual - expected) > tolerance:
        raise ExperimentError(
            f"{label}明显缺段：实际 {actual:.3f}s，目标约 {expected:.3f}s，允许 {tolerance:.3f}s"
        )


def _decode_spots(
    ffmpeg: Path,
    media: Path,
    positions: Iterable[tuple[str, float]],
    log_path: Path,
) -> list[dict[str, Any]]:
    _require_file(ffmpeg, "ffmpeg")
    results: list[dict[str, Any]] = []
    with log_path.open("a", encoding="utf-8", newline="") as log:
        for label, position in positions:
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                f"{max(0.0, position):.3f}",
                "-i",
                str(media),
                "-map",
                "0:v:0",
                "-t",
                "1",
                "-f",
                "null",
                os.devnull,
            ]
            started = time.perf_counter()
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            log.write(f"spot={label} command={subprocess.list2cmdline(command)}\n")
            log.write(result.stdout)
            log.write(result.stderr)
            log.write(f"return_code={result.returncode} elapsed_seconds={elapsed:.3f}\n")
            if result.returncode != 0:
                raise ExperimentError(f"{label} 的约 1 秒解码检查失败；日志：{log_path}")
            results.append({"label": label, "position_seconds": max(0.0, position), "elapsed_seconds": elapsed})
    return results


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _verify_short(plan: ExperimentPlan) -> dict[str, Any]:
    source_probe = _probe(plan.paths.ffprobe, plan.paths.short_input)
    output_probe = _probe(plan.paths.ffprobe, plan.paths.short_output)
    source_stream = _video_stream(source_probe)
    output_stream = _video_stream(output_probe)
    if (source_stream.get("width"), source_stream.get("height"), source_stream.get("avg_frame_rate")) != (
        output_stream.get("width"),
        output_stream.get("height"),
        output_stream.get("avg_frame_rate"),
    ):
        raise ExperimentError("短样输入与输出的分辨率或平均帧率不一致。")
    source_duration = _duration(source_probe)
    output_duration = _duration(output_probe)
    _assert_close_duration(output_duration, source_duration, "短样输出")
    spots = _decode_spots(
        plan.paths.ffmpeg,
        plan.paths.short_output,
        (
            ("start", 0.0),
            ("middle", max(0.0, output_duration / 2.0 - 0.5)),
            ("end", max(0.0, output_duration - 1.0)),
        ),
        plan.paths.logs_dir / "verify-short.log",
    )
    payload = {
        "status": "technical_checks_passed",
        "scope": "仅技术检查；不代表人眼画质验收通过",
        "input_duration_seconds": source_duration,
        "output_duration_seconds": output_duration,
        "output_video_stream": output_stream,
        "decode_spots": spots,
    }
    _write_json(plan.paths.logs_dir / "verify-short.json", payload)
    return payload


def _ffconcat_line(path: Path) -> str:
    normalized = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{normalized}'"


def _write_concat_manifest(plan: ExperimentPlan) -> None:
    content = "\n".join(
        (_ffconcat_line(plan.paths.long_output_a), _ffconcat_line(plan.paths.long_output_b))
    ) + "\n"
    plan.paths.concat_manifest.write_text(content, encoding="utf-8")


def _verify_long(plan: ExperimentPlan) -> dict[str, Any]:
    input_probe_a = _probe(plan.paths.ffprobe, plan.paths.long_input_a)
    input_probe_b = _probe(plan.paths.ffprobe, plan.paths.long_input_b)
    probe_a = _probe(plan.paths.ffprobe, plan.paths.long_output_a)
    probe_b = _probe(plan.paths.ffprobe, plan.paths.long_output_b)
    final_probe = _probe(plan.paths.ffprobe, plan.paths.final_output)
    input_stream_a = _video_stream(input_probe_a)
    input_stream_b = _video_stream(input_probe_b)
    stream_a = _video_stream(probe_a)
    stream_b = _video_stream(probe_b)
    final_stream = _video_stream(final_probe)
    signature = _stream_signature(stream_a)
    if _stream_signature(stream_b) != signature or _stream_signature(final_stream) != signature:
        raise ExperimentError("A、B 与拼接成品的视频编码参数不一致，不能判定为可靠的 stream-copy 拼接。")
    input_duration_a = _duration(input_probe_a)
    input_duration_b = _duration(input_probe_b)
    duration_a = _duration(probe_a)
    duration_b = _duration(probe_b)
    if (input_stream_a.get("width"), input_stream_a.get("height"), input_stream_a.get("avg_frame_rate")) != (
        stream_a.get("width"),
        stream_a.get("height"),
        stream_a.get("avg_frame_rate"),
    ):
        raise ExperimentError("A 段输入与输出的分辨率或平均帧率不一致。")
    if (input_stream_b.get("width"), input_stream_b.get("height"), input_stream_b.get("avg_frame_rate")) != (
        stream_b.get("width"),
        stream_b.get("height"),
        stream_b.get("avg_frame_rate"),
    ):
        raise ExperimentError("B 段输入与输出的分辨率或平均帧率不一致。")
    _assert_close_duration(duration_a, input_duration_a, "A 段输出")
    _assert_close_duration(duration_b, input_duration_b, "B 段输出")
    _assert_extracted_total(input_duration_a + input_duration_b, 30 * 60, "30 分钟输入总量")
    final_duration = _duration(final_probe)
    expected = duration_a + duration_b
    _assert_close_duration(final_duration, expected, "拼接成品")
    seam = min(max(0.0, duration_a - 0.5), max(0.0, final_duration - 1.0))
    spots = _decode_spots(
        plan.paths.ffmpeg,
        plan.paths.final_output,
        (
            ("start", 0.0),
            ("seam", seam),
            ("end", max(0.0, final_duration - 1.0)),
        ),
        plan.paths.logs_dir / "verify-long.log",
    )
    payload = {
        "status": "technical_checks_passed",
        "scope": "仅技术检查；接缝与去马画质仍需用户观看验收",
        "segment_a_input_duration_seconds": input_duration_a,
        "segment_b_input_duration_seconds": input_duration_b,
        "segment_a_duration_seconds": duration_a,
        "segment_b_duration_seconds": duration_b,
        "segment_sum_seconds": expected,
        "final_duration_seconds": final_duration,
        "final_video_stream": final_stream,
        "decode_spots": spots,
    }
    _write_json(plan.paths.logs_dir / "verify-long.json", payload)
    return payload


def _validate_video_only(ffprobe: Path, path: Path) -> dict[str, Any]:
    payload = _probe(ffprobe, path)
    _video_stream(payload)
    _duration(payload)
    return payload


def _execute_command_step(plan: ExperimentPlan, step: ExperimentStep) -> dict[str, Any]:
    assert step.command is not None
    paths = plan.paths
    if step.key == "prepare-short":
        _require_file(paths.ffmpeg, "ffmpeg")
        _require_file(paths.source, "源视频")
        _require_outputs_absent(step.expected_outputs)
    elif step.key == "prepare-long":
        _require_file(paths.ffmpeg, "ffmpeg")
        _require_file(paths.source, "源视频")
        existing = sorted(paths.inputs_dir.glob("long_*.mkv")) if paths.inputs_dir.exists() else []
        _require_outputs_absent(existing)
    elif step.key == "process-short":
        _require_file(paths.jasna, "Jasna")
        _ensure_ascii_install_path(paths.jasna)
        _validate_video_only(paths.ffprobe, paths.short_input)
        _require_outputs_absent(step.expected_outputs)
    elif step.key == "process-long-a":
        _require_file(paths.jasna, "Jasna")
        _ensure_ascii_install_path(paths.jasna)
        _validate_video_only(paths.ffprobe, paths.long_input_a)
        _require_outputs_absent(step.expected_outputs)
    elif step.key == "process-long-b":
        _require_file(paths.jasna, "Jasna")
        _ensure_ascii_install_path(paths.jasna)
        _validate_video_only(paths.ffprobe, paths.long_input_b)
        _require_outputs_absent(step.expected_outputs)
    elif step.key == "concat-long":
        _require_file(paths.ffmpeg, "ffmpeg")
        probe_a = _validate_video_only(paths.ffprobe, paths.long_output_a)
        probe_b = _validate_video_only(paths.ffprobe, paths.long_output_b)
        if _stream_signature(_video_stream(probe_a)) != _stream_signature(_video_stream(probe_b)):
            raise ExperimentError("A/B 视频参数不同，拒绝执行 stream-copy 拼接。")
        _require_outputs_absent(step.expected_outputs)
        _write_concat_manifest(plan)

    paths.inputs_dir.mkdir(parents=True, exist_ok=True)
    paths.outputs_dir.mkdir(parents=True, exist_ok=True)
    elapsed = _run_streamed(
        step.command,
        _log_path(plan, step),
        stop_on_software_decode=step.key in GPU_STEPS,
        timeout_seconds=SHORT_PROCESS_TIMEOUT_SECONDS if step.key == "process-short" else None,
    )
    for expected in step.expected_outputs:
        _require_file(expected, "预期输出")
        if expected.stat().st_size <= 0:
            raise ExperimentError(f"预期输出为空文件：{expected}")

    if step.key == "prepare-long":
        actual = sorted(paths.inputs_dir.glob("long_*.mkv"))
        expected = [paths.long_input_a, paths.long_input_b]
        if actual != expected:
            raise ExperimentError(
                "30 分钟抽取没有恰好产生 long_00.mkv 与 long_01.mkv；"
                f"实际为 {[str(item) for item in actual]}"
            )
        probe_a = _validate_video_only(paths.ffprobe, paths.long_input_a)
        probe_b = _validate_video_only(paths.ffprobe, paths.long_input_b)
        _assert_extracted_total(_duration(probe_a) + _duration(probe_b), 30 * 60, "30 分钟输入总量")

    if step.key == "process-short":
        input_probe = _validate_video_only(paths.ffprobe, paths.short_input)
        output_probe = _validate_video_only(paths.ffprobe, paths.short_output)
        _assert_close_duration(_duration(output_probe), _duration(input_probe), "短样输出")

    if step.key == "process-long-a":
        input_probe = _validate_video_only(paths.ffprobe, paths.long_input_a)
        output_probe = _validate_video_only(paths.ffprobe, paths.long_output_a)
        _assert_close_duration(_duration(output_probe), _duration(input_probe), "A 段输出")

    if step.key == "process-long-b":
        input_probe = _validate_video_only(paths.ffprobe, paths.long_input_b)
        output_probe = _validate_video_only(paths.ffprobe, paths.long_output_b)
        _assert_close_duration(_duration(output_probe), _duration(input_probe), "B 段输出")

    return {
        "status": "completed",
        "step": step.key,
        "elapsed_seconds": elapsed,
        "outputs": [str(path) for path in step.expected_outputs],
        "log": str(_log_path(plan, step)),
    }


def _require_short_gate(plan: ExperimentPlan) -> None:
    report_path = plan.paths.logs_dir / "verify-short.json"
    _require_file(report_path, "短样技术检查报告")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"短样技术检查报告无效：{report_path}") from exc
    if report.get("status") != "technical_checks_passed":
        raise ExperimentError(f"短样技术检查尚未通过：{report_path}")


def execute_step(
    plan: ExperimentPlan,
    key: str,
    *,
    confirm_gpu_ready: bool = False,
    confirm_engines_ready: bool = False,
    confirm_short_viewed: bool = False,
) -> dict[str, Any]:
    step = plan.step(key)
    if step.requires_gpu_confirmation and not confirm_gpu_ready:
        raise ExperimentError(
            "该步骤会使用 GPU。只有在确认 Whisper A/C 已结束并释放 GPU 后，才可重试并显式传入 "
            "--confirm-gpu-ready。"
        )
    if step.requires_gpu_confirmation and not confirm_engines_ready:
        raise ExperimentError(
            "该步骤的耗时将用于判断吞吐。请先在安装任务中完成 TensorRT 首次编译，再显式传入 "
            "--confirm-engines-ready；冷启动的 15–60 分钟不能混入短样计时。"
        )
    if key in LONG_STEPS:
        _require_short_gate(plan)
        if not confirm_short_viewed:
            raise ExperimentError(
                "30 分钟阶段要求短样技术检查已通过且用户已观看认可。确认后显式传入 "
                "--confirm-short-viewed。"
            )
    if key == "verify-short":
        return _verify_short(plan)
    if key == "verify-long":
        return _verify_long(plan)
    return _execute_command_step(plan, step)
