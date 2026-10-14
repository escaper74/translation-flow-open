"""Strict, lossless-enough SRT contracts used by the production flow."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


_TIMECODE_RE = re.compile(
    r"^(?P<sh>\d{2,}):(?P<sm>[0-5]\d):(?P<ss>[0-5]\d),(?P<sms>\d{3})"
    r" --> "
    r"(?P<eh>\d{2,}):(?P<em>[0-5]\d):(?P<es>[0-5]\d),(?P<ems>\d{3})$"
)


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def validate_positive_durations(
    cues: Sequence[SubtitleCue], *, label: str = "SRT"
) -> None:
    """Require every cue to occupy a presentable, positive-duration interval."""

    invalid_ids = [cue.index for cue in cues if cue.end_ms <= cue.start_ms]
    if invalid_ids:
        raise ValueError(
            f"{label} contains non-positive-duration cues: {invalid_ids}"
        )


def subtitle_digest(cues: list[SubtitleCue]) -> str:
    canonical = json.dumps(
        [[cue.index, cue.start_ms, cue.end_ms, cue.text] for cue in cues],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_ms(hours: str, minutes: str, seconds: str, milliseconds: str) -> int:
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def _format_ms(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid SRT timestamp milliseconds: {value!r}")
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def parse_srt(path: str | Path) -> list[SubtitleCue]:
    """Parse an SRT without silently skipping malformed blocks."""

    source = Path(path)
    raw = source.read_text(encoding="utf-8-sig")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip("\n")) if normalized else []
    if not blocks or all(not block.strip() for block in blocks):
        raise ValueError(f"SRT contains no cues: {source}")

    cues: list[SubtitleCue] = []
    seen_ids: set[int] = set()
    for position, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 2:
            raise ValueError(f"malformed SRT block {position}: expected index and timecode")
        index_line = lines[0].strip()
        if not re.fullmatch(r"[1-9]\d*", index_line):
            raise ValueError(f"invalid SRT index in block {position}: {lines[0]!r}")
        index = int(index_line)
        if index in seen_ids:
            raise ValueError(f"duplicate SRT index: {index}")

        match = _TIMECODE_RE.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError(f"invalid SRT timecode at #{index}: {lines[1]!r}")
        start_ms = _to_ms(
            match["sh"], match["sm"], match["ss"], match["sms"]
        )
        end_ms = _to_ms(match["eh"], match["em"], match["es"], match["ems"])
        text = "\n".join(lines[2:]).strip()
        cues.append(SubtitleCue(index, start_ms, end_ms, text))
        seen_ids.add(index)

    validate_positive_durations(cues, label=f"SRT {source}")
    return cues


def write_srt(path: str | Path, cues: list[SubtitleCue]) -> Path:
    """Write cues as UTF-8 SRT, validating IDs and timestamps first."""

    target = Path(path)
    if not cues:
        raise ValueError("cannot write an empty SRT")
    validate_positive_durations(cues, label="SRT output")
    seen_ids: set[int] = set()
    blocks: list[str] = []
    for cue in cues:
        if not isinstance(cue.index, int) or isinstance(cue.index, bool) or cue.index <= 0:
            raise ValueError(f"invalid SRT index: {cue.index!r}")
        if cue.index in seen_ids:
            raise ValueError(f"duplicate SRT index: {cue.index}")
        text = cue.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if re.search(r"\n[ \t]*\n", text):
            raise ValueError(f"SRT cue #{cue.index} contains a blank text line")
        blocks.append(
            f"{cue.index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n{text}"
        )
        seen_ids.add(cue.index)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return target


def validate_timeline(
    backbone: list[SubtitleCue], candidate: list[SubtitleCue]
) -> None:
    """Require exactly the same cue count, IDs, and timestamps."""

    validate_positive_durations(backbone, label="subtitle backbone")
    validate_positive_durations(candidate, label="subtitle candidate")
    if len(backbone) != len(candidate):
        raise ValueError(
            f"subtitle count mismatch: expected {len(backbone)}, got {len(candidate)}"
        )
    for position, (expected, actual) in enumerate(zip(backbone, candidate), start=1):
        if expected.index != actual.index:
            raise ValueError(
                f"subtitle ID mismatch at position {position}: "
                f"expected {expected.index}, got {actual.index}"
            )
        if (expected.start_ms, expected.end_ms) != (actual.start_ms, actual.end_ms):
            raise ValueError(
                f"subtitle timecode mismatch at #{expected.index}: "
                f"expected {expected.start_ms}-{expected.end_ms}, "
                f"got {actual.start_ms}-{actual.end_ms}"
            )
