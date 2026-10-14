#!/usr/bin/env python3
"""
Clean common Whisper hallucination blocks from SRT output and renumber entries.
The raw SRT is kept by the caller; this script writes the cleaned SRT.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Block:
    index: int
    start: str
    end: str
    text: list[str]


COMMON_HALLUCINATIONS = {
    "この動画の字幕は視聴者のみによって作成されました",
    "この動画の字幕は視聴者のみによって作成されました。",
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "ありがとうございました",
    "ありがとうございました。",
    "thanks for watching",
    "thank you for watching",
}


def parse_time(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d+):(\d+),(\d+)", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def normalize_text(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def comparable_text(lines: list[str]) -> str:
    return normalize_text(lines).casefold().strip(" \t\r\n.,!?。！？、，")


def normalized_common_hallucinations() -> set[str]:
    return {text.casefold().strip(" \t\r\n.,!?。！？、，") for text in COMMON_HALLUCINATIONS}


def parse_srt(path: Path) -> list[Block]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    for block in [part.strip() for part in re.split(r"\n{2,}", raw) if part.strip()]:
        lines = block.split("\n")
        if len(lines) < 2:
            continue
        index = int(lines[0].strip())
        start, end = [part.strip() for part in lines[1].split("-->", 1)]
        blocks.append(Block(index=index, start=start, end=end, text=lines[2:]))
    return blocks


def should_drop(block: Block) -> tuple[bool, str]:
    text = normalize_text(block.text)
    duration = parse_time(block.end) - parse_time(block.start)
    comparable = comparable_text(block.text)
    common = normalized_common_hallucinations()
    if not text:
        return True, "empty cue cannot be displayed"
    if comparable in common and duration >= 8:
        return True, "common long no-speech hallucination"
    if duration >= 20 and len(text) <= 16 and comparable in common:
        return True, "very long common hallucination"
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 80:
        counts = {char: compact.count(char) for char in set(compact)}
        top_count = max(counts.values()) if counts else 0
        unique_chars = len(counts)
        if unique_chars <= 6 or top_count / len(compact) >= 0.55:
            return True, "long repetitive character hallucination"
    return False, ""


def repeated_common_runs(blocks: list[Block], min_run: int = 3) -> dict[int, str]:
    common = normalized_common_hallucinations()
    drop_indexes: dict[int, str] = {}
    current_text = ""
    current_run: list[Block] = []

    def flush_run() -> None:
        if len(current_run) >= min_run and current_text in common:
            for block in current_run:
                drop_indexes[block.index] = "repeated common no-speech hallucination"

    for block in blocks:
        text = comparable_text(block.text)
        if text and text == current_text:
            current_run.append(block)
        else:
            flush_run()
            current_text = text
            current_run = [block] if text else []
    flush_run()
    return drop_indexes


def repeated_generic_runs(blocks: list[Block], min_run: int = 6) -> dict[int, str]:
    drop_indexes: dict[int, str] = {}
    current_text = ""
    current_run: list[Block] = []

    def flush_run() -> None:
        if len(current_run) >= min_run and len(current_text) >= 5:
            for block in current_run:
                drop_indexes[block.index] = "repeated same-line hallucination"

    for block in blocks:
        text = comparable_text(block.text)
        if text and text == current_text:
            current_run.append(block)
        else:
            flush_run()
            current_text = text
            current_run = [block] if text else []
    flush_run()
    return drop_indexes


def non_positive_duration_blocks(blocks: list[Block]) -> dict[int, str]:
    """Drop intervals that no subtitle renderer can present.

    Preserve the more specific duplicate reason when Whisper emitted the same
    text at an adjacent boundary.  Other zero/negative intervals are still
    recorded verbatim in the cleanup report, but they no longer stop an entire
    media job after local transcription has completed.
    """

    drop_indexes: dict[int, str] = {}
    for position, block in enumerate(blocks):
        start = parse_time(block.start)
        end = parse_time(block.end)
        text = normalize_text(block.text).casefold()
        if end > start:
            continue

        if end == start and text:
            neighbors: list[tuple[Block, bool]] = []
            if position > 0:
                neighbors.append((blocks[position - 1], True))
            if position + 1 < len(blocks):
                neighbors.append((blocks[position + 1], False))
            for neighbor, is_previous in neighbors:
                neighbor_start = parse_time(neighbor.start)
                neighbor_end = parse_time(neighbor.end)
                neighbor_text = normalize_text(neighbor.text).casefold()
                shares_boundary = (
                    neighbor_end == start if is_previous else neighbor_start == start
                )
                if (
                    neighbor_end > neighbor_start
                    and shares_boundary
                    and neighbor_text == text
                ):
                    drop_indexes[block.index] = (
                        "zero-duration exact duplicate at adjacent boundary"
                    )
                    break
        if block.index not in drop_indexes:
            drop_indexes[block.index] = (
                "zero-duration cue cannot be displayed"
                if end == start
                else "negative-duration cue cannot be displayed"
            )
    return drop_indexes


def clean_blocks(blocks: list[Block]) -> tuple[list[Block], list[dict]]:
    kept: list[Block] = []
    dropped: list[dict] = []
    run_drop_indexes = repeated_common_runs(blocks)
    run_drop_indexes.update(repeated_generic_runs(blocks))
    run_drop_indexes.update(non_positive_duration_blocks(blocks))
    for block in blocks:
        drop, reason = should_drop(block)
        if block.index in run_drop_indexes:
            drop = True
            reason = run_drop_indexes[block.index]
        if drop:
            dropped.append(
                {
                    "index": block.index,
                    "start": block.start,
                    "end": block.end,
                    "text": normalize_text(block.text),
                    "reason": reason,
                }
            )
        else:
            kept.append(block)
    return kept, dropped


def write_srt(path: Path, blocks: list[Block]) -> None:
    parts: list[str] = []
    for new_index, block in enumerate(blocks, start=1):
        text = "\n".join(line.strip() for line in block.text if line.strip())
        parts.append(f"{new_index}\n{block.start} --> {block.end}\n{text}\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean common Whisper SRT hallucinations.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    blocks = parse_srt(args.input)
    kept, dropped = clean_blocks(blocks)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_srt(args.output, kept)
    report = {
        "input": str(args.input),
        "output": str(args.output),
        "input_blocks": len(blocks),
        "kept_blocks": len(kept),
        "dropped_blocks": dropped,
    }
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
