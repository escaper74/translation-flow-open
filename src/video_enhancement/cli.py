import argparse
import json
import os
from pathlib import Path
import sys
from translation_flow.events import EventEmitter
from translation_flow.cancellation import interrupts
from translation_flow.translation.codex_provider import TranslationCancelled
from .jobs import SRRequest, SRRunner


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description="本地轻量超分及可选的 15 秒对照")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--tool-root", type=Path)
    args = parser.parse_args(argv)
    os.environ.pop("SUBTITLE_API_KEY", None)
    events = None
    try:
        request = SRRequest.from_mapping(json.loads(args.request.read_text(encoding="utf-8-sig")))
        events = EventEmitter(request.job_id, request.output_dir / ".super-resolution" / request.job_id / "events.jsonl")
        with interrupts() as cancelled:
            SRRunner(request, tools=args.tool_root, emitter=events, cancelled=cancelled).run()
        return 0
    except Exception as error:
        stopped = isinstance(error, TranslationCancelled)
        if events:
            events.emit("job_cancelled" if stopped else "job_failed", error_code=type(error).__name__,
                        message=str(error), recovery_hint="保留原片和完成段；修正原因后继续同一任务。")
        else:
            sys.stderr.write(str(error) + "\n")
        return 2 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
