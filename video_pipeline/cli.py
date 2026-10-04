from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_RIFE_CHUNK_FRAMES, PipelineOptions
from .runner import PipelineRequest, VideoPipelineRunner


def parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("布尔值必须是 true 或 false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本机纯画面处理流水线")
    parser.add_argument("--input-video", type=Path, required=True)
    parser.add_argument("--output-video", type=Path, required=True)
    parser.add_argument("--enable-demosaic", type=parse_bool, required=True)
    parser.add_argument(
        "--enable-quality-enhancement", type=parse_bool, required=True
    )
    parser.add_argument(
        "--enable-frame-interpolation-2x", type=parse_bool, required=True
    )
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "workflow" / "tools",
    )
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--cleanup-on-success", type=parse_bool, default=True)
    parser.add_argument(
        "--rife-chunk-frames", type=int, default=DEFAULT_RIFE_CHUNK_FRAMES
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    options = PipelineOptions(
        enable_demosaic=args.enable_demosaic,
        enable_quality_enhancement=args.enable_quality_enhancement,
        enable_frame_interpolation_2x=args.enable_frame_interpolation_2x,
    )
    request = PipelineRequest(
        input_video=args.input_video,
        output_video=args.output_video,
        options=options,
        tool_root=args.tool_root,
        job_dir=args.job_dir,
        cleanup_on_success=args.cleanup_on_success,
        rife_chunk_frames=args.rife_chunk_frames,
    )
    try:
        VideoPipelineRunner(request).run()
        return 0
    except Exception as exc:
        # 正常路径由 runner 输出 job_failed；这里只保证极早期错误也有机器可读结果。
        if not isinstance(exc, (SystemExit, KeyboardInterrupt)):
            sys.stderr.write(
                json.dumps(
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
