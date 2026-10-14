#!/usr/bin/env python3
"""
Fail fast when an SRT looks like a Whisper repetition collapse.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Block:
    index: int
    start: str
    end: str
    text: str


def normalize_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip(" \t\r\n.,!?。！？、，")


def parse_srt(path: Path) -> list[Block]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    for part in [chunk.strip() for chunk in re.split(r"\n{2,}", raw) if chunk.strip()]:
        lines = part.split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        start, end = [value.strip() for value in lines[1].split("-->", 1)]
        text = " ".join(line.strip() for line in lines[2:] if line.strip())
        blocks.append(Block(index=index, start=start, end=end, text=text))
    return blocks


def build_report(blocks: list[Block]) -> dict:
    normalized = [normalize_text(block.text) for block in blocks]
    counted = [text for text in normalized if len(text) >= 5]
    counts = Counter(counted)
    top_text = ""
    top_count = 0
    if counts:
        top_text, top_count = counts.most_common(1)[0]

    max_run_text = ""
    max_run = 0
    current_text = ""
    current_run = 0
    for text in normalized:
        if len(text) < 5:
            text = ""
        if text and text == current_text:
            current_run += 1
        else:
            if current_run > max_run:
                max_run = current_run
                max_run_text = current_text
            current_text = text
            current_run = 1 if text else 0
    if current_run > max_run:
        max_run = current_run
        max_run_text = current_text

    ratio_base = max(len(blocks), 1)
    return {
        "blocks": len(blocks),
        "counted_blocks": len(counted),
        "top_text": top_text,
        "top_count": top_count,
        "top_ratio": top_count / ratio_base,
        "max_consecutive_text": max_run_text,
        "max_consecutive_count": max_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SRT quality.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-repeat-ratio", type=float, default=0.18)
    parser.add_argument("--min-repeat-count", type=int, default=50)
    parser.add_argument("--max-consecutive", type=int, default=12)
    args = parser.parse_args()

    blocks = parse_srt(args.input)
    report = build_report(blocks)
    failures: list[str] = []
    if report["top_count"] >= args.min_repeat_count and report["top_ratio"] >= args.max_repeat_ratio:
        failures.append(
            f"top repeated text ratio is too high: {report['top_count']}/{report['blocks']} "
            f"({report['top_ratio']:.1%}) text={report['top_text']!r}"
        )
    if report["max_consecutive_count"] >= args.max_consecutive:
        failures.append(
            f"same text repeats too many times consecutively: "
            f"{report['max_consecutive_count']} text={report['max_consecutive_text']!r}"
        )

    report["failures"] = failures
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
