from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .workflow import FinalMuxConfig, FinalMuxError, FinalMuxWorkflow
from translation_flow.runtime import tool_root


DEFAULT_FFMPEG = tool_root() / "ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"
DEFAULT_FFPROBE = DEFAULT_FFMPEG.with_name("ffprobe.exe")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "最终薄汇合：已处理视频轨 + 原片对应音轨，"
            "可选全源时间轴中文字幕。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("plan", "只读检查输入并打印准确计划，不生成文件。"),
        ("run", "执行字幕裁切平移、一次软封装和技术检查。"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--processed-video", required=True, type=Path)
        child.add_argument("--source-media", required=True, type=Path)
        subtitle = child.add_mutually_exclusive_group(required=True)
        subtitle.add_argument("--chinese-srt", type=Path)
        subtitle.add_argument(
            "--no-subtitle",
            action="store_true",
            help="不添加软字幕；用于源画面已含硬字幕的画面处理交付。",
        )
        child.add_argument(
            "--source-start-seconds",
            required=True,
            help="处理后视频在原片中的真实起点秒数，可带小数。",
        )
        child.add_argument("--output", required=True, type=Path)
        child.add_argument("--work-dir", required=True, type=Path)
        child.add_argument(
            "--seam-position-seconds",
            action="append",
            default=[],
            help="相对最终样片 0 轴的接缝秒数；可重复传入。",
        )
        child.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
        child.add_argument("--ffprobe", type=Path, default=DEFAULT_FFPROBE)
    return parser


def _config(args: argparse.Namespace) -> FinalMuxConfig:
    return FinalMuxConfig(
        processed_video=args.processed_video,
        source_media=args.source_media,
        chinese_srt=None if args.no_subtitle else args.chinese_srt,
        source_start_seconds=args.source_start_seconds,
        output=args.output,
        work_dir=args.work_dir,
        seam_positions_seconds=tuple(args.seam_position_seconds),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workflow = FinalMuxWorkflow(args.ffmpeg, args.ffprobe)
    try:
        result = (
            workflow.plan(_config(args))
            if args.command == "plan"
            else workflow.run(_config(args))
        )
    except (FinalMuxError, FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
