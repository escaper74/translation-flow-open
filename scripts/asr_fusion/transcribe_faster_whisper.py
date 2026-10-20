#!/usr/bin/env python3
"""Run local faster-whisper and deterministically rebuild subtitle cues."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


SENTENCE_ENDINGS = set("。！？!?…")
BREAK_PUNCTUATION = set("、，。！？!?：:；;…")
NO_SPACE_BEFORE = set("、，。！？!?：:；;）)]】』」〉》…")
NO_SPACE_AFTER = set("（([【『「〈《")


@dataclass(frozen=True)
class WordStamp:
    start: float
    end: float
    text: str
    probability: float | None = None


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    word_count: int


def _visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _is_ascii_word_edge(character: str) -> bool:
    return character.isascii() and character.isalnum()


def append_token(base: str, token: str) -> str:
    token = re.sub(r"\s+", " ", token).strip()
    if not token:
        return base
    if not base:
        return token
    if token[0] in NO_SPACE_BEFORE or base[-1] in NO_SPACE_AFTER:
        return base + token
    if _is_ascii_word_edge(base[-1]) and _is_ascii_word_edge(token[0]):
        return base + " " + token
    return base + token


def _cue_from_words(words: Sequence[WordStamp]) -> Cue:
    text = ""
    for word in words:
        text = append_token(text, word.text)
    return Cue(
        start=max(0.0, words[0].start),
        end=max(words[0].start + 0.01, words[-1].end),
        text=text.strip(),
        word_count=len(words),
    )


def build_cues(
    words: Iterable[WordStamp],
    *,
    max_chars: int = 28,
    max_duration: float = 6.0,
    max_gap: float = 0.65,
    min_sentence_duration: float = 0.8,
) -> list[Cue]:
    """Group word timestamps into stable cues with deterministic limits."""

    ordered = sorted(
        (
            word
            for word in words
            if word.text.strip()
            and math.isfinite(word.start)
            and math.isfinite(word.end)
            and word.end >= word.start
        ),
        key=lambda item: (item.start, item.end, item.text),
    )
    cues: list[Cue] = []
    current: list[WordStamp] = []

    for word in ordered:
        if current:
            proposed = _cue_from_words([*current, word])
            gap = max(0.0, word.start - current[-1].end)
            should_split = (
                gap > max_gap
                or proposed.end - proposed.start > max_duration
                or _visible_length(proposed.text) > max_chars
            )
            if should_split:
                cues.append(_cue_from_words(current))
                current = []

        current.append(word)
        candidate = _cue_from_words(current)
        if (
            candidate.text
            and candidate.text[-1] in SENTENCE_ENDINGS
            and candidate.end - candidate.start >= min_sentence_duration
        ):
            cues.append(candidate)
            current = []

    if current:
        cues.append(_cue_from_words(current))
    return cues


def stabilize_timings(
    cues: Sequence[Cue], *, min_duration: float = 0.8, min_gap: float = 0.05
) -> list[Cue]:
    """Extend short cues when space allows, without introducing overlaps."""

    stabilized: list[Cue] = []
    for index, cue in enumerate(cues):
        start = max(0.0, cue.start)
        natural_end = max(start + 0.01, cue.end)
        next_start = cues[index + 1].start if index + 1 < len(cues) else None
        desired_end = max(natural_end, start + min_duration)
        if next_start is not None:
            desired_end = min(desired_end, max(natural_end, next_start - min_gap))
        if stabilized:
            previous = stabilized[-1]
            start = max(start, previous.end + min_gap)
            desired_end = max(desired_end, start + 0.01)
            if next_start is not None:
                desired_end = min(desired_end, max(start + 0.01, next_start - min_gap))
        stabilized.append(
            Cue(start=start, end=desired_end, text=cue.text, word_count=cue.word_count)
        )
    return stabilized


def build_segment_aware_cues(
    segment_words: Sequence[Sequence[WordStamp]],
    segment_texts: Sequence[str],
    *,
    max_chars: int = 28,
    max_line_chars: int = 16,
    max_duration: float = 6.0,
    max_gap: float = 0.65,
) -> list[Cue]:
    """Resegment inside model segments without breaking short semantic phrases.

    Faster-whisper can assign multi-second gaps between word timestamps that still
    belong to one short decoded phrase. For a phrase that already fits one cue,
    model-segment membership is the stronger boundary, but only up to twice the
    normal duration limit. This hard ceiling prevents a short hallucinated phrase
    from remaining visible across a long music or silence interval. Punctuation may
    still split preserved phrases.
    """

    if len(segment_words) != len(segment_texts):
        raise ValueError("segment word/text counts differ")
    cues: list[Cue] = []
    for words, decoded_text in zip(segment_words, segment_texts, strict=True):
        if not words:
            continue
        semantic_capacity = max(max_chars, max_line_chars * 2)
        finite_words = [
            word
            for word in words
            if math.isfinite(word.start)
            and math.isfinite(word.end)
            and word.end >= word.start
        ]
        segment_duration = (
            max(word.end for word in finite_words)
            - min(word.start for word in finite_words)
            if finite_words
            else float("inf")
        )
        preserved_duration_ceiling = max_duration * 2.0
        short_semantic_segment = (
            _visible_length(decoded_text) <= semantic_capacity
            and segment_duration <= preserved_duration_ceiling
        )
        cues.extend(
            build_cues(
                words,
                max_chars=semantic_capacity if short_semantic_segment else max_chars,
                max_duration=float("inf") if short_semantic_segment else max_duration,
                max_gap=float("inf") if short_semantic_segment else max_gap,
            )
        )
    return cues


def wrap_text(text: str, *, max_line_chars: int = 16) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if _visible_length(text) <= max_line_chars:
        return [text]

    candidates: list[int] = []
    for index, character in enumerate(text[:-1], start=1):
        if character in BREAK_PUNCTUATION or character == " ":
            candidates.append(index)
    def line_lengths(position: int) -> tuple[int, int]:
        return (
            _visible_length(text[:position].strip()),
            _visible_length(text[position:].strip()),
        )

    def fits(position: int) -> bool:
        first_length, second_length = line_lengths(position)
        return first_length <= max_line_chars and second_length <= max_line_chars

    valid_preferred = [position for position in candidates if fits(position)]
    valid_fallback = [position for position in range(1, len(text)) if fits(position)]
    choices = valid_preferred or valid_fallback or candidates or list(range(1, len(text)))
    split_at = min(
        choices,
        key=lambda value: (
            abs(line_lengths(value)[0] - line_lengths(value)[1]),
            value,
        ),
    )
    first = text[:split_at].strip()
    second = text[split_at:].strip()
    return [line for line in (first, second) if line]


def format_timestamp(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(path: Path, cues: Sequence[Cue], *, max_line_chars: int = 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = "\n".join(wrap_text(cue.text, max_line_chars=max_line_chars))
        blocks.append(
            f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{text}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _fallback_word(segment: object) -> WordStamp | None:
    text = str(getattr(segment, "text", "")).strip()
    if not text:
        return None
    return WordStamp(
        start=float(getattr(segment, "start", 0.0)),
        end=float(getattr(segment, "end", 0.0)),
        text=text,
        probability=None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local faster-whisper transcription with deterministic SRT resegmentation."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--raw-json", required=True, type=Path)
    parser.add_argument("--model-srt", required=True, type=Path)
    parser.add_argument("--output-srt", required=True, type=Path)
    parser.add_argument("--output-txt", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad-threshold", type=float, default=0.35)
    parser.add_argument("--min-speech-ms", type=int, default=100)
    parser.add_argument("--min-silence-ms", type=int, default=500)
    parser.add_argument("--speech-pad-ms", type=int, default=250)
    parser.add_argument("--max-chars", type=int, default=28)
    parser.add_argument("--max-line-chars", type=int, default=16)
    parser.add_argument("--max-duration", type=float, default=6.0)
    parser.add_argument("--max-gap", type=float, default=0.65)
    args = parser.parse_args()

    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if not args.model.is_dir():
        raise FileNotFoundError(args.model)

    from faster_whisper import WhisperModel

    started = time.perf_counter()
    model = WhisperModel(
        str(args.model), device=args.device, compute_type=args.compute_type
    )
    segment_iterator, info = model.transcribe(
        str(args.input),
        language="ja",
        task="transcribe",
        beam_size=args.beam_size,
        temperature=0.0,
        vad_filter=True,
        vad_parameters={
            "threshold": args.vad_threshold,
            "min_speech_duration_ms": args.min_speech_ms,
            "min_silence_duration_ms": args.min_silence_ms,
            "speech_pad_ms": args.speech_pad_ms,
        },
        word_timestamps=True,
        initial_prompt="Japanese spoken dialogue. Keep proper nouns and short utterances accurate.",
        condition_on_previous_text=True,
    )
    segments = list(segment_iterator)
    elapsed = time.perf_counter() - started

    model_cues: list[Cue] = []
    words: list[WordStamp] = []
    segment_word_groups: list[list[WordStamp]] = []
    segment_texts: list[str] = []
    raw_segments: list[dict] = []
    for segment in segments:
        segment_text = str(segment.text).strip()
        model_cues.append(
            Cue(
                start=float(segment.start),
                end=float(segment.end),
                text=segment_text,
                word_count=len(segment.words or []),
            )
        )
        raw_words: list[dict] = []
        segment_stamps: list[WordStamp] = []
        for word in segment.words or []:
            if word.start is None or word.end is None:
                continue
            stamp = WordStamp(
                start=float(word.start),
                end=float(word.end),
                text=str(word.word),
                probability=float(word.probability) if word.probability is not None else None,
            )
            words.append(stamp)
            segment_stamps.append(stamp)
            raw_words.append(asdict(stamp))
        if not raw_words:
            fallback = _fallback_word(segment)
            if fallback is not None:
                words.append(fallback)
                segment_stamps.append(fallback)
                raw_words.append(asdict(fallback))
        segment_word_groups.append(segment_stamps)
        segment_texts.append(segment_text)
        raw_segments.append(
            {
                "id": int(segment.id),
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment_text,
                "avg_logprob": float(segment.avg_logprob),
                "no_speech_prob": float(segment.no_speech_prob),
                "words": raw_words,
            }
        )

    cues = stabilize_timings(
        build_segment_aware_cues(
            segment_word_groups,
            segment_texts,
            max_chars=args.max_chars,
            max_line_chars=args.max_line_chars,
            max_duration=args.max_duration,
            max_gap=args.max_gap,
        )
    )
    write_srt(args.model_srt, model_cues, max_line_chars=args.max_line_chars)
    write_srt(args.output_srt, cues, max_line_chars=args.max_line_chars)
    args.output_txt.parent.mkdir(parents=True, exist_ok=True)
    args.output_txt.write_text("\n".join(cue.text for cue in cues) + "\n", encoding="utf-8")

    raw_payload = {
        "language": info.language,
        "language_probability": float(info.language_probability),
        "duration": float(info.duration),
        "duration_after_vad": float(info.duration_after_vad),
        "segments": raw_segments,
    }
    args.raw_json.parent.mkdir(parents=True, exist_ok=True)
    args.raw_json.write_text(
        json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = {
        "engine": "faster-whisper",
        "versions": {
            "faster-whisper": _version("faster-whisper"),
            "ctranslate2": _version("ctranslate2"),
        },
        "model": str(args.model),
        "device": args.device,
        "compute_type": args.compute_type,
        "elapsed_seconds": round(elapsed, 3),
        "language": info.language,
        "language_probability": float(info.language_probability),
        "duration": float(info.duration),
        "duration_after_vad": float(info.duration_after_vad),
        "model_segments": len(model_cues),
        "word_timestamps": len(words),
        "resegmented_cues": len(cues),
        "vad": {
            "threshold": args.vad_threshold,
            "min_speech_duration_ms": args.min_speech_ms,
            "min_silence_duration_ms": args.min_silence_ms,
            "speech_pad_ms": args.speech_pad_ms,
        },
        "resegmentation": {
            "implementation": "repo deterministic word-timestamp grouping",
            "max_chars": args.max_chars,
            "max_line_chars": args.max_line_chars,
            "max_duration_seconds": args.max_duration,
            "max_preserved_duration_seconds": args.max_duration * 2.0,
            "max_gap_seconds": args.max_gap,
            "boundary_policy": "preserve model segments that fit the two-line hard capacity only up to twice max duration; otherwise enforce word gaps and duration",
            "stable_ts": "not used; archived reference only",
        },
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
