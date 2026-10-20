from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import JobRequest
from .events import EventEmitter
from .runner import TranslationFlowRunner
from .translation.codex_provider import codex_status, TranslationCancelled
from .cancellation import interrupts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="正式日语影片字幕与 MKV 翻译流")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", type=Path)
    mode.add_argument("--codex-status", action="store_true")
    parser.add_argument("--tool-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    request: JobRequest | None = None
    emitter: EventEmitter | None = None
    try:
        api_key = os.environ.pop("SUBTITLE_API_KEY", "")
        if args.codex_status:
            print(json.dumps(codex_status(), ensure_ascii=False))
            return 0
        payload = json.loads(args.request.read_text(encoding="utf-8-sig"))
        request = JobRequest.from_mapping(payload)
        job_root = request.output_dir / ".translation-flow" / request.job_id
        emitter = EventEmitter(request.job_id, job_root / "events.jsonl")
        with interrupts() as cancelled:
            TranslationFlowRunner(
                request, api_key, tool_root=args.tool_root,
                emitter=emitter, cancelled=cancelled,
            ).run()
        return 0
    except TranslationCancelled as exc:
        if emitter is not None:
            emitter.emit("job_cancelled", error_code="TranslationCancelled", message=str(exc),
                         retryable=True, recovery_hint="从同一任务重新确认并继续，成功批次会复用。")
        return 2
    except Exception as exc:
        if emitter is not None:
            emitter.emit(
                "job_failed",
                error_code=type(exc).__name__,
                message=str(exc),
                retryable=False,
                recovery_hint=(
                    "保留任务记录；修正原因后从正式入口重新确认本次授权。"
                    "相同输入、输出和模式会使用同一任务检查点续跑。"
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
