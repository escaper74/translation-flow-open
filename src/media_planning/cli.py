from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from translation_flow.events import EventEmitter
from translation_flow.cancellation import check_cancelled, interrupts
from translation_flow.translation.codex_provider import TranslationCancelled

from .contracts import MediaRepackageRequest
from .runner import MediaRepackageRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="已有中文字幕影片的本地 MKV/MP4 重新封装"
    )
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--tool-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    request: MediaRepackageRequest | None = None
    emitter: EventEmitter | None = None
    try:
        os.environ.pop("SUBTITLE_API_KEY", None)
        payload = json.loads(args.request.read_text(encoding="utf-8-sig"))
        request = MediaRepackageRequest.from_mapping(payload)
        emitter = EventEmitter(request.job_id, request.job_root / "events.jsonl")
        (request.job_root / "cancel.requested").unlink(missing_ok=True)
        with interrupts() as cancelled:
            check_cancelled(cancelled)
            MediaRepackageRunner(
                request,
                tool_root=args.tool_root,
                emitter=emitter,
                cancelled=cancelled,
            ).run()
        return 0
    except TranslationCancelled as exc:
        if emitter is not None:
            emitter.emit("job_cancelled", error_code="TranslationCancelled", message=str(exc),
                         retryable=True, recovery_hint="用同一请求继续；已发布的成品与报告会保留。")
        return 2
    except Exception as exc:
        if emitter is not None:
            emitter.emit(
                "job_failed",
                error_code=type(exc).__name__,
                message=str(exc),
                retryable=False,
                recovery_hint=(
                    "源媒体和已有结果未被覆盖；按提示改用 MKV 或更换输出目录后重试。"
                ),
            )
        else:
            sys.stderr.write(
                json.dumps(
                    {"error_type": type(exc).__name__, "message": str(exc)},
                    ensure_ascii=False,
                )
                + "\n"
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
