"""Formal picture-only desktop/queue entry point; no translation credentials."""
import argparse
import json
import os
from pathlib import Path
import sys
from translation_flow.events import EventEmitter
from translation_flow.cancellation import interrupts, TranslationCancelled
from .picture import PictureRequest, PictureRunner


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='去马并封装原声音与已有字幕')
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--tool-root', type=Path)
    parser.add_argument('--jasna', type=Path)
    parser.add_argument('--acceptance-gate-report', type=Path)
    args = parser.parse_args(argv)
    os.environ.pop('SUBTITLE_API_KEY', None)
    emitter = None
    try:
        request = PictureRequest.from_mapping(json.loads(args.request.read_text(encoding='utf-8-sig')))
        emitter = EventEmitter(request.job_id, request.output_dir / '.picture-workflow' / request.job_id / 'events.jsonl')
        with interrupts() as cancelled:
            PictureRunner(request, tools=args.tool_root, jasna=args.jasna, gate=args.acceptance_gate_report,
                          emitter=emitter, cancelled=cancelled).run()
        return 0
    except Exception as exc:
        stopped = isinstance(exc, TranslationCancelled)
        if emitter:
            emitter.emit('job_cancelled' if stopped else 'job_failed', message=str(exc),
                         error_code=type(exc).__name__, retryable=True,
                         recovery_hint='已完成去马段和报告保留；从同一任务继续。')
        else:
            print(json.dumps(dict(error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2 if stopped else 1


if __name__ == '__main__':
    raise SystemExit(main())
