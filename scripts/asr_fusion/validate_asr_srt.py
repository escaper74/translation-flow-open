#!/usr/bin/env python3
"""Validate structural constraints for local ASR experiment SRT files."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


TIMECODE = re.compile(
    r"^(?P<sh>\d+):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})\s+-->\s+"
    r"(?P<eh>\d+):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
)


@dataclass(frozen=True)
class Block:
    index: int
    start: float
    end: float
    lines: tuple[str, ...]


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_srt(path: Path) -> list[Block]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in re.split(r"\n{2,}", raw) if part.strip()]
    blocks: list[Block] = []
    for position, part in enumerate(parts, start=1):
        lines = part.splitlines()
        if len(lines) < 2:
            raise ValueError(f"block {position} has fewer than two lines")
        try:
            index = int(lines[0])
        except ValueError as exc:
            raise ValueError(f"block {position} has invalid index: {lines[0]!r}") from exc
        match = TIMECODE.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"block {position} has invalid timecode: {lines[1]!r}")
        groups = match.groupdict()
        start = _seconds(groups["sh"], groups["sm"], groups["ss"], groups["sms"])
        end = _seconds(groups["eh"], groups["em"], groups["es"], groups["ems"])
        blocks.append(Block(index=index, start=start, end=end, lines=tuple(lines[2:])))
    return blocks


def validate_blocks(
    blocks: list[Block],
    *,
    source_duration: float,
    max_lines: int = 2,
    max_line_chars: int = 0,
    max_cue_duration: float = 0.0,
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    max_duration = 0.0
    max_visible_line_chars = 0
    previous_end = 0.0
    for expected_index, block in enumerate(blocks, start=1):
        if block.index != expected_index:
            failures.append(f"non-sequential index at block {expected_index}: {block.index}")
        if block.start < 0 or block.end <= block.start:
            failures.append(f"invalid interval at block {block.index}: {block.start:.3f}-{block.end:.3f}")
        if expected_index > 1 and block.start < previous_end:
            warnings.append(f"overlap at block {block.index}: starts {block.start:.3f} before {previous_end:.3f}")
        if block.end > source_duration + 0.5:
            warnings.append(f"block {block.index} exceeds source duration: {block.end:.3f}")
        if not block.lines or not any(line.strip() for line in block.lines):
            failures.append(f"empty text at block {block.index}")
        if len(block.lines) > max_lines:
            warnings.append(f"block {block.index} has {len(block.lines)} lines (max {max_lines})")
        for line in block.lines:
            visible = len(re.sub(r"\s+", "", line))
            max_visible_line_chars = max(max_visible_line_chars, visible)
            if max_line_chars > 0 and visible > max_line_chars:
                warnings.append(
                    f"block {block.index} line has {visible} visible chars (max {max_line_chars})"
                )
        cue_duration = block.end - block.start
        max_duration = max(max_duration, cue_duration)
        if max_cue_duration > 0 and cue_duration > max_cue_duration:
            warnings.append(
                f"block {block.index} lasts {cue_duration:.3f}s (max {max_cue_duration:.3f}s)"
            )
        previous_end = block.end
    return {
        "blocks": len(blocks),
        "first_start": blocks[0].start if blocks else None,
        "last_end": blocks[-1].end if blocks else None,
        "max_cue_duration": round(max_duration, 3),
        "max_lines": max((len(block.lines) for block in blocks), default=0),
        "max_visible_line_chars": max_visible_line_chars,
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-duration", required=True, type=float)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--max-line-chars", type=int, default=0)
    parser.add_argument("--max-cue-duration", type=float, default=0.0)
    args = parser.parse_args()
    try:
        blocks = parse_srt(args.input)
        report = validate_blocks(
            blocks,
            source_duration=args.source_duration,
            max_lines=args.max_lines,
            max_line_chars=args.max_line_chars,
            max_cue_duration=args.max_cue_duration,
        )
    except Exception as exc:
        report = {"blocks": 0, "failures": [str(exc)], "warnings": []}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
