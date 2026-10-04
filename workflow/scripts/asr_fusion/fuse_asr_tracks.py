#!/usr/bin/env python3
"""Build a conservative A+C Japanese subtitle track.

A is the completeness backbone. C may replace an A cue only when the pairing is
unambiguously one-to-one, strongly aligned in time, and textually consistent.
The output therefore cannot lose an A cue merely because C is silent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from acceptance.srt_utils import Cue, parse_srt, structure_failures


SPACE_RE = re.compile(r"\s+")
@dataclass(frozen=True)
class Match:
    a_index: int
    c_index: int
    overlap_ms: int
    containment: float
    duration_ratio: float
    length_ratio: float
    sequence_similarity: float
    character_f1: float
    text_similarity: float
    score: float
    accepted: bool
    reason: str


@dataclass(frozen=True)
class OutputCue:
    start_ms: int
    end_ms: int
    text: str
    source: str
    source_a_index: int
    source_c_index: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-srt", required=True, type=Path)
    parser.add_argument("--c-srt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--min-containment", type=float, default=0.65)
    parser.add_argument("--min-text-similarity", type=float, default=0.48)
    parser.add_argument("--strong-containment", type=float, default=0.82)
    parser.add_argument("--relaxed-text-similarity", type=float, default=0.38)
    parser.add_argument("--min-character-f1", type=float, default=0.50)
    return parser.parse_args()


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = SPACE_RE.sub("", text)
    return "".join(
        char
        for char in text
        if not unicodedata.category(char).startswith(("P", "S"))
    )


def text_scores(left: str, right: str) -> tuple[float, float, float]:
    a = normalized(left)
    b = normalized(right)
    if not a or not b:
        return 0.0, 0.0, 0.0
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    common = sum((Counter(a) & Counter(b)).values())
    precision = common / len(b)
    recall = common / len(a)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return sequence, f1, max(sequence, f1)


def overlap_ms(left: Cue, right: Cue) -> int:
    return max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))


def significant_overlap(left: Cue, right: Cue) -> bool:
    overlap = overlap_ms(left, right)
    shorter = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return shorter > 0 and overlap / shorter >= 0.35


def build_matches(a_cues: list[Cue], c_cues: list[Cue], args: argparse.Namespace) -> list[Match]:
    significant_by_a = {
        cue.index: [other for other in c_cues if significant_overlap(cue, other)]
        for cue in a_cues
    }
    significant_by_c = {
        cue.index: [other for other in a_cues if significant_overlap(other, cue)]
        for cue in c_cues
    }
    matches: list[Match] = []

    for a_cue in a_cues:
        candidates = significant_by_a[a_cue.index]
        if not candidates:
            continue
        for c_cue in candidates:
            overlap = overlap_ms(a_cue, c_cue)
            a_duration = a_cue.end_ms - a_cue.start_ms
            c_duration = c_cue.end_ms - c_cue.start_ms
            containment = overlap / min(a_duration, c_duration)
            duration_ratio = c_duration / a_duration
            a_length = len(normalized(a_cue.text))
            c_length = len(normalized(c_cue.text))
            length_ratio = c_length / max(a_length, 1)
            sequence, character_f1, similarity = text_scores(a_cue.text, c_cue.text)
            center_distance = abs(
                (a_cue.start_ms + a_cue.end_ms) - (c_cue.start_ms + c_cue.end_ms)
            ) / 2
            center_score = max(0.0, 1.0 - center_distance / max(a_duration, c_duration, 1))
            score = 0.50 * containment + 0.30 * similarity + 0.20 * center_score

            accepted = True
            reason = "accepted_unique_one_to_one"
            if len(candidates) != 1 or len(significant_by_c[c_cue.index]) != 1:
                accepted = False
                reason = "ambiguous_segmentation"
            elif containment < args.min_containment:
                accepted = False
                reason = "weak_time_containment"
            elif not 0.55 <= duration_ratio <= 1.80:
                accepted = False
                reason = "duration_ratio_out_of_range"
            elif not 0.50 <= length_ratio <= 2.00:
                accepted = False
                reason = "text_length_ratio_out_of_range"
            elif not (
                similarity >= args.min_text_similarity
                or (
                    containment >= args.strong_containment
                    and similarity >= args.relaxed_text_similarity
                    and character_f1 >= args.min_character_f1
                )
            ):
                accepted = False
                reason = "weak_japanese_text_agreement"

            matches.append(
                Match(
                    a_index=a_cue.index,
                    c_index=c_cue.index,
                    overlap_ms=overlap,
                    containment=round(containment, 6),
                    duration_ratio=round(duration_ratio, 6),
                    length_ratio=round(length_ratio, 6),
                    sequence_similarity=round(sequence, 6),
                    character_f1=round(character_f1, 6),
                    text_similarity=round(similarity, 6),
                    score=round(score, 6),
                    accepted=accepted,
                    reason=reason,
                )
            )
    return matches


def timecode(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(path: Path, cues: list[OutputCue]) -> None:
    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n{timecode(cue.start_ms)} --> {timecode(cue.end_ms)}\n"
            + cue.text
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    for destination in (args.output, args.report):
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {destination}")

    started = time.perf_counter()
    a_cues = parse_srt(args.a_srt)
    c_cues = parse_srt(args.c_srt)
    a_failures = structure_failures(a_cues)
    c_failures = structure_failures(c_cues)
    if a_failures or c_failures:
        raise ValueError(f"input structure failures: A={a_failures} C={c_failures}")

    matches = build_matches(a_cues, c_cues, args)
    accepted_by_a = {match.a_index: match for match in matches if match.accepted}
    c_by_index = {cue.index: cue for cue in c_cues}
    output_cues = []
    for cue in a_cues:
        match = accepted_by_a.get(cue.index)
        replacement = c_by_index[match.c_index] if match else None
        selected_text = replacement.text if replacement else cue.text
        output_cues.append(
            OutputCue(
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=selected_text.strip(),
                source="C" if replacement else "A",
                source_a_index=cue.index,
                source_c_index=replacement.index if replacement else None,
            )
        )

    write_srt(args.output, output_cues)
    parsed_output = parse_srt(args.output)
    output_failures = structure_failures(parsed_output)
    timeline_preserved = len(parsed_output) == len(a_cues) and all(
        output.start_ms == backbone.start_ms and output.end_ms == backbone.end_ms
        for output, backbone in zip(parsed_output, a_cues)
    )
    if not timeline_preserved:
        output_failures.append("fusion output timeline differs from A backbone")

    reasons = Counter(match.reason for match in matches if not match.accepted)
    matched_a_indices = {match.a_index for match in matches}
    matched_c_indices = {match.c_index for match in matches}
    report = {
        "status": "complete" if not output_failures else "failed",
        "policy": {
            "backbone": "A",
            "replacement": "C only for unique one-to-one time alignment with Japanese text agreement",
            "missing_c_never_removes_a": True,
            "accepted_c_uses_a_timing": True,
            "timeline_changes": "forbidden in fusion; optional resegmentation is a separate post-process",
            "parameters": {
                "min_containment": args.min_containment,
                "min_text_similarity": args.min_text_similarity,
                "strong_containment": args.strong_containment,
                "relaxed_text_similarity": args.relaxed_text_similarity,
                "min_character_f1": args.min_character_f1,
            },
        },
        "inputs": {
            "a_srt": str(args.a_srt.resolve()),
            "a_sha256": sha256(args.a_srt),
            "a_cues": len(a_cues),
            "c_srt": str(args.c_srt.resolve()),
            "c_sha256": sha256(args.c_srt),
            "c_cues": len(c_cues),
        },
        "output": {
            "srt": str(args.output.resolve()),
            "sha256": sha256(args.output),
            "cues": len(output_cues),
            "a_backbone_cues": len(a_cues) - len(accepted_by_a),
            "c_replacements": len(accepted_by_a),
            "timeline_preserved_from_a": timeline_preserved,
            "structure_failures": output_failures,
        },
        "unmatched_a_indices": [cue.index for cue in a_cues if cue.index not in matched_a_indices],
        "unmatched_c_indices": [cue.index for cue in c_cues if cue.index not in matched_c_indices],
        "rejection_reasons": dict(sorted(reasons.items())),
        "accepted_matches": [asdict(match) for match in matches if match.accepted],
        "rejected_matches": [asdict(match) for match in matches if not match.accepted],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    atomic_json(args.report, report)
    print(json.dumps({"status": report["status"], **report["output"]}, ensure_ascii=False))
    return 1 if output_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
