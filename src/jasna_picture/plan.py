from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any


SHORT_START = "01:34:00"
SHORT_SECONDS = 60
LONG_START = "01:30:00"
LONG_SECONDS = 30 * 60
LONG_SPLIT_SECONDS = 15 * 60


@dataclass(frozen=True)
class ExperimentPaths:
    source: Path
    work_dir: Path
    jasna: Path
    ffmpeg: Path
    ffprobe: Path

    @property
    def inputs_dir(self) -> Path:
        return self.work_dir / "inputs"

    @property
    def outputs_dir(self) -> Path:
        return self.work_dir / "outputs"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def short_input(self) -> Path:
        return self.inputs_dir / "short_sample.mkv"

    @property
    def long_input_a(self) -> Path:
        return self.inputs_dir / "long_00.mkv"

    @property
    def long_input_b(self) -> Path:
        return self.inputs_dir / "long_01.mkv"

    @property
    def long_input_pattern(self) -> Path:
        return self.inputs_dir / "long_%02d.mkv"

    @property
    def short_output(self) -> Path:
        return self.outputs_dir / "short_restored.mkv"

    @property
    def long_output_a(self) -> Path:
        return self.outputs_dir / "long_00_restored.mkv"

    @property
    def long_output_b(self) -> Path:
        return self.outputs_dir / "long_01_restored.mkv"

    @property
    def concat_manifest(self) -> Path:
        return self.work_dir / "concat_long.txt"

    @property
    def final_output(self) -> Path:
        return self.outputs_dir / "acceptance_restored_video_only.mkv"


@dataclass(frozen=True)
class ExperimentStep:
    key: str
    description: str
    command: tuple[str, ...] | None
    expected_outputs: tuple[Path, ...]
    requires_gpu_confirmation: bool = False
    kind: str = "command"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "description": self.description,
            "kind": self.kind,
            "requires_gpu_confirmation": self.requires_gpu_confirmation,
            "expected_outputs": [str(path) for path in self.expected_outputs],
        }
        if self.command is not None:
            result["argv"] = list(self.command)
            result["windows_command_line"] = subprocess.list2cmdline(self.command)
        return result


@dataclass(frozen=True)
class ExperimentPlan:
    paths: ExperimentPaths
    steps: tuple[ExperimentStep, ...]

    def step(self, key: str) -> ExperimentStep:
        for item in self.steps:
            if item.key == key:
                return item
        raise KeyError(f"Unknown experiment step: {key}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": "Jasna 画面去马独立实验；不处理音频、字幕或翻译",
            "baseline": {
                "jasna_release": "v0.10.0",
                "restoration_model": "basicvsrpp",
                "detection_model": "lada-yolo-v4",
                "max_clip_size_frames": 90,
                "temporal_overlap_frames": 8,
                "crossfade": True,
                "scene_detection": True,
                "secondary_restoration": "none",
                "denoise": "none",
                "codec": "hevc",
                "cq": 28,
            },
            "ranges": {
                "short": {"start": SHORT_START, "requested_seconds": SHORT_SECONDS},
                "long": {
                    "start": LONG_START,
                    "requested_seconds": LONG_SECONDS,
                    "single_split_at_seconds": LONG_SPLIT_SECONDS,
                },
            },
            "hard_gates": [
                "GPU 步骤必须显式传入 --confirm-gpu-ready；调用者负责确认 Whisper A/C 已结束并释放 GPU。",
                "GPU 步骤还必须显式确认 TensorRT 引擎已在安装任务中完成编译，冷启动不进入吞吐计时。",
                "Jasna 日志必须是 info；若检测到 FFmpeg 软件解码回退，runner 会立刻终止该步骤。",
                "60 秒短样未通过技术检查与用户观看前，不进入 30 分钟实验。",
                "任一步骤失败即停止；不会自动跳步、自动删除失败产物或冒充成功。",
            ],
            "paths": {
                "source": str(self.paths.source),
                "work_dir": str(self.paths.work_dir),
                "jasna": str(self.paths.jasna),
                "ffmpeg": str(self.paths.ffmpeg),
                "ffprobe": str(self.paths.ffprobe),
                "final_video_only": str(self.paths.final_output),
            },
            "steps": [step.as_dict() for step in self.steps],
        }


def _ffmpeg_prefix(ffmpeg: Path) -> list[str]:
    return [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-nostdin", "-n"]


def build_jasna_command(
    jasna: Path,
    input_path: Path,
    output_path: Path,
) -> tuple[str, ...]:
    return (
        str(jasna),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--device",
        "cuda:0",
        "--batch-size",
        "4",
        "--fp16",
        "--restoration-model-name",
        "basicvsrpp",
        "--compile-basicvsrpp",
        "--max-clip-size",
        "90",
        "--temporal-overlap",
        "8",
        "--enable-crossfade",
        "--denoise",
        "none",
        "--detection-model",
        "lada-yolo-v4",
        "--max-detection-gap",
        "2",
        "--min-detection-duration",
        "2",
        "--scene-detection",
        "--secondary-restoration",
        "none",
        "--vr-mode",
        "off",
        "--codec",
        "hevc",
        "--cq",
        "28",
        "--post-export-action",
        "none",
        "--log-level",
        "info",
    )


def build_experiment_plan(paths: ExperimentPaths) -> ExperimentPlan:
    short_prepare = tuple(
        _ffmpeg_prefix(paths.ffmpeg)
        + [
            "-ss",
            SHORT_START,
            "-i",
            str(paths.source),
            "-t",
            str(SHORT_SECONDS),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            str(paths.short_input),
        ]
    )
    long_prepare = tuple(
        _ffmpeg_prefix(paths.ffmpeg)
        + [
            "-ss",
            LONG_START,
            "-i",
            str(paths.source),
            "-t",
            str(LONG_SECONDS),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            "-f",
            "segment",
            "-segment_times",
            str(LONG_SPLIT_SECONDS),
            "-reset_timestamps",
            "1",
            str(paths.long_input_pattern),
        ]
    )
    concat = tuple(
        _ffmpeg_prefix(paths.ffmpeg)
        + [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(paths.concat_manifest),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            str(paths.final_output),
        ]
    )

    steps = (
        ExperimentStep(
            "prepare-short",
            "从 01:34:00 附近流复制约 60 秒，只保留视频轨。",
            short_prepare,
            (paths.short_input,),
        ),
        ExperimentStep(
            "process-short",
            "Jasna 直接把短样编码为最终 HEVC 视频段。",
            build_jasna_command(paths.jasna, paths.short_input, paths.short_output),
            (paths.short_output,),
            requires_gpu_confirmation=True,
        ),
        ExperimentStep(
            "verify-short",
            "FFprobe 检查单视频轨、时长/画面参数，并解码开头/中部/结尾各约 1 秒。",
            None,
            (paths.logs_dir / "verify-short.json",),
            kind="verification",
        ),
        ExperimentStep(
            "prepare-long",
            "从 01:30:00 连续流复制 30 分钟，只在约 15 分钟处按关键帧切一次。",
            long_prepare,
            (paths.long_input_a, paths.long_input_b),
        ),
        ExperimentStep(
            "process-long-a",
            "Jasna 直接处理第一段约 15 分钟视频。",
            build_jasna_command(paths.jasna, paths.long_input_a, paths.long_output_a),
            (paths.long_output_a,),
            requires_gpu_confirmation=True,
        ),
        ExperimentStep(
            "process-long-b",
            "Jasna 直接处理第二段约 15 分钟视频。",
            build_jasna_command(paths.jasna, paths.long_input_b, paths.long_output_b),
            (paths.long_output_b,),
            requires_gpu_confirmation=True,
        ),
        ExperimentStep(
            "concat-long",
            "按 A、B 顺序写入 concat 清单并使用 stream copy 拼接，不做整片重编码。",
            concat,
            (paths.final_output,),
            kind="concat",
        ),
        ExperimentStep(
            "verify-long",
            "检查 A/B/成品均为单视频轨、参数一致、总时长接近，并解码开头/接缝/结尾各约 1 秒。",
            None,
            (paths.logs_dir / "verify-long.json",),
            kind="verification",
        ),
    )
    return ExperimentPlan(paths=paths, steps=steps)
