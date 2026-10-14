from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from translation_flow.runtime import tool_root, jasna_path

from .plan import ExperimentPaths, build_experiment_plan
from .runner import ALL_STEPS, ExperimentError, execute_step
from .workflow import (
    DEFAULT_SEGMENT_SECONDS,
    PictureWorkflowConfig,
    preflight_workflow,
    run_picture_workflow,
    workflow_plan,
)


DEFAULT_JASNA = jasna_path(tools=tool_root())
DEFAULT_FFMPEG = tool_root() / "ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"
DEFAULT_FFPROBE = DEFAULT_FFMPEG.with_name("ffprobe.exe")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--jasna", type=Path, default=DEFAULT_JASNA)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", type=Path, default=DEFAULT_FFPROBE)


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", required=True, choices=("acceptance", "full"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gate-report", required=True, type=Path)
    parser.add_argument(
        "--start",
        help="30 分钟 acceptance 的起点；full 不允许传入。",
    )
    parser.add_argument(
        "--segment-seconds",
        type=int,
        default=DEFAULT_SEGMENT_SECONDS,
        help="整片段长，默认 900 秒；acceptance 固定为 900 秒。",
    )
    parser.add_argument("--jasna", type=Path, default=DEFAULT_JASNA)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", type=Path, default=DEFAULT_FFPROBE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jasna 纯画面 workflow；实验步骤和正式分段处理均不会碰音频、字幕或翻译。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="打印实验计划和准确命令，不执行。")
    _add_common_arguments(plan_parser)

    run_parser = subparsers.add_parser("run", help="只执行一个显式指定的步骤。")
    _add_common_arguments(run_parser)
    run_parser.add_argument("--step", required=True, choices=ALL_STEPS)
    run_parser.add_argument(
        "--confirm-gpu-ready",
        action="store_true",
        help="确认 Whisper A/C 已结束并释放 GPU；仅 GPU 步骤需要。",
    )
    run_parser.add_argument(
        "--confirm-engines-ready",
        action="store_true",
        help="确认 TensorRT 首次编译已在安装任务中完成；仅 GPU 步骤需要。",
    )
    run_parser.add_argument(
        "--confirm-short-viewed",
        action="store_true",
        help="确认短样技术检查通过且用户已观看认可；所有 30 分钟步骤需要。",
    )

    workflow_plan_parser = subparsers.add_parser(
        "workflow-plan",
        help="打印正式画面 workflow 的配置、引擎清单和命令模板，不读媒体。",
    )
    _add_workflow_arguments(workflow_plan_parser)

    workflow_preflight_parser = subparsers.add_parser(
        "workflow-preflight",
        help="零 GPU 核对工具版本、七个 TensorRT 引擎、源片和前级报告。",
    )
    _add_workflow_arguments(workflow_preflight_parser)

    workflow_run_parser = subparsers.add_parser(
        "workflow-run",
        help="顺序执行纯视频切片、Jasna、stream-copy 拼接和最低技术检查。",
    )
    _add_workflow_arguments(workflow_run_parser)
    workflow_run_parser.add_argument(
        "--confirm-gpu-ready",
        action="store_true",
        help="确认 Whisper A/C 已退出且本轮不会干扰现有 GPU 任务。",
    )
    workflow_run_parser.add_argument(
        "--confirm-gate-viewed",
        action="store_true",
        help="确认用户已观看并认可 gate-report 对应的前级样片。",
    )
    return parser


def _build_plan(args: argparse.Namespace):
    paths = ExperimentPaths(
        source=args.source,
        work_dir=args.work_dir,
        jasna=args.jasna,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
    )
    return build_experiment_plan(paths)


def _build_workflow_config(args: argparse.Namespace) -> PictureWorkflowConfig:
    return PictureWorkflowConfig(
        scope=args.scope,
        source=args.source,
        work_dir=args.work_dir,
        output=args.output,
        jasna=args.jasna,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        gate_report=args.gate_report,
        start=args.start,
        segment_seconds=args.segment_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        plan = _build_plan(args)
        print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "workflow-plan":
        try:
            result = workflow_plan(_build_workflow_config(args))
        except ExperimentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "workflow-preflight":
        try:
            result = preflight_workflow(_build_workflow_config(args))
        except ExperimentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "workflow-run":
        try:
            result = run_picture_workflow(
                _build_workflow_config(args),
                confirm_gpu_ready=args.confirm_gpu_ready,
                confirm_gate_viewed=args.confirm_gate_viewed,
            )
        except ExperimentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    plan = _build_plan(args)
    try:
        result = execute_step(
            plan,
            args.step,
            confirm_gpu_ready=args.confirm_gpu_ready,
            confirm_engines_ready=args.confirm_engines_ready,
            confirm_short_viewed=args.confirm_short_viewed,
        )
    except ExperimentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
