#!/usr/bin/env python3
"""Replay deterministic subtitle grouping from saved faster-whisper words."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from transcribe_faster_whisper import (
    Cue,
    WordStamp,
    build_segment_aware_cues,
    stabilize_timings,
    write_srt,
)


def _destination(path: Path) -> Path:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-json", required=True, type=Path)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--output-raw-json", required=True, type=Path)
    parser.add_argument("--model-srt", required=True, type=Path)
    parser.add_argument("--output-srt", required=True, type=Path)
    parser.add_argument("--output-txt", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--max-chars", type=int, default=28)
    parser.add_argument("--max-line-chars", type=int, default=16)
    parser.add_argument("--max-duration", type=float, default=6.0)
    parser.add_argument("--max-gap", type=float, default=0.65)
    args = parser.parse_args()

    started = time.perf_counter()
    raw_bytes = args.raw_json.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    source_report = json.loads(args.source_report.read_text(encoding="utf-8-sig"))

    segment_words: list[list[WordStamp]] = []
    segment_texts: list[str] = []
    model_cues: list[Cue] = []
    for segment in payload["segments"]:
        words = [
            WordStamp(
                start=float(word["start"]),
                end=float(word["end"]),
                text=str(word["text"]),
                probability=(
                    None if word.get("probability") is None else float(word["probability"])
                ),
            )
            for word in segment.get("words", [])
        ]
        text = str(segment.get("text", "")).strip()
        segment_words.append(words)
        segment_texts.append(text)
        if text:
            model_cues.append(
                Cue(
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                    text=text,
                    word_count=len(words),
                )
            )

    cues = stabilize_timings(
        build_segment_aware_cues(
            segment_words,
            segment_texts,
            max_chars=args.max_chars,
            max_line_chars=args.max_line_chars,
            max_duration=args.max_duration,
            max_gap=args.max_gap,
        )
    )

    output_raw_json = _destination(args.output_raw_json)
    model_srt = _destination(args.model_srt)
    output_srt = _destination(args.output_srt)
    output_txt = _destination(args.output_txt)
    report_path = _destination(args.report)
    shutil.copyfile(args.raw_json, output_raw_json)
    write_srt(model_srt, model_cues, max_line_chars=args.max_line_chars)
    write_srt(output_srt, cues, max_line_chars=args.max_line_chars)
    output_txt.write_text("\n".join(cue.text for cue in cues) + "\n", encoding="utf-8")

    replay = source_report.get("resegmentation", {}).copy()
    replay.update(
        {
            "max_chars": args.max_chars,
            "max_line_chars": args.max_line_chars,
            "max_duration_seconds": args.max_duration,
            "max_preserved_duration_seconds": args.max_duration * 2.0,
            "max_gap_seconds": args.max_gap,
            "boundary_policy": "preserve model segments that fit the two-line hard capacity only up to twice max duration; otherwise enforce word gaps and duration",
            "replayed_from_saved_word_timestamps": True,
            "source_raw_json_sha256": hashlib.sha256(raw_bytes).hexdigest().upper(),
        }
    )
    report = source_report.copy()
    report.update(
        {
            "model_segments": len(model_cues),
            "word_timestamps": sum(len(words) for words in segment_words),
            "resegmented_cues": len(cues),
            "resegmentation": replay,
            "resegmentation_replay_elapsed_seconds": round(
                time.perf_counter() - started, 3
            ),
        }
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
