from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from translation_flow.events import EventEmitter
from translation_flow.translation.codex_provider import TranslationCancelled
from translation_flow.cancellation import interrupts

from .contracts import FullWorkflowRequest
from .runner import FullWorkflowRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="整片翻译、画面处理与最终薄汇合 workflow")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--tool-root", type=Path)
    parser.add_argument("--acceptance-gate-report", type=Path)
    parser.add_argument("--jasna", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    emitter: EventEmitter | None = None
    try:
        api_key = os.environ.pop("SUBTITLE_API_KEY", "")
        payload = json.loads(args.request.read_text(encoding="utf-8-sig"))
        request = FullWorkflowRequest.from_mapping(payload)
        workflow_root = (
            request.translation.output_dir
            / ".full-workflow"
            / request.translation.job_id
        )
        emitter = EventEmitter(
            request.translation.job_id,
            workflow_root / "events.jsonl",
        )
        kwargs: dict[str, object] = {
            "tool_root": args.tool_root,
            "emitter": emitter,
        }
        if args.acceptance_gate_report is not None:
            kwargs["acceptance_gate_report"] = args.acceptance_gate_report
        if args.jasna is not None:
            kwargs["jasna"] = args.jasna
        with interrupts() as cancelled:
            FullWorkflowRunner(request, api_key, cancelled=cancelled, **kwargs).run()
        return 0
    except TranslationCancelled as exc:
        if emitter is not None:
            emitter.emit("job_cancelled", error_code="TranslationCancelled", message=str(exc),
                         retryable=True, recovery_hint="成功批次及已启动分支的成果已保留；从同一请求继续。")
        return 2
    except Exception as exc:
        if emitter is not None:
            emitter.emit(
                "job_failed",
                error_code=type(exc).__name__,
                message=str(exc),
                retryable=False,
                recovery_hint=(
                    "保留三阶段报告与产物；修正原因后从同一正式入口重新确认，"
                    "workflow 只复用与本次谱系匹配的完成阶段。"
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
    finally:
        os.environ.pop("SUBTITLE_API_KEY", None)


if __name__ == "__main__":
    raise SystemExit(main())
