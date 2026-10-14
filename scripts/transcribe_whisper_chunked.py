#!/usr/bin/env python3
"""
Transcribe a long video in short Whisper chunks, then merge SRT timecodes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Block:
    start: float
    end: float
    lines: list[str]


def run_command(args: list[str]) -> None:
    print("+ " + " ".join(str(arg) for arg in args), flush=True)
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, errors="replace")
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed with exit code {completed.returncode}: {args[0]}")


def capture_command(args: list[str]) -> str:
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"command failed: {args[0]}")
    return completed.stdout.strip()


def hardlink_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def parse_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d+):(\d+),(\d+)", value.strip())
    if not match:
        raise ValueError(f"invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def format_timestamp(seconds: float) -> str:
    millis_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(millis_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(path: Path, offset: float) -> list[Block]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    for part in [chunk.strip() for chunk in re.split(r"\n{2,}", raw) if chunk.strip()]:
        lines = part.split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_text, end_text = [value.strip() for value in lines[1].split("-->", 1)]
        text_lines = [line.strip() for line in lines[2:] if line.strip()]
        if not text_lines:
            continue
        blocks.append(
            Block(
                start=parse_timestamp(start_text) + offset,
                end=parse_timestamp(end_text) + offset,
                lines=text_lines,
            )
        )
    return blocks


def write_srt(path: Path, blocks: list[Block]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for index, block in enumerate(blocks, start=1):
        text = "\n".join(block.lines)
        parts.append(
            f"{index}\n{format_timestamp(block.start)} --> {format_timestamp(block.end)}\n{text}\n"
        )
    path.write_text("\n".join(parts), encoding="utf-8")


def ffprobe_duration(ffprobe: Path, input_video: Path) -> float:
    output = capture_command(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_video),
        ]
    )
    return float(output)


def main() -> int:
    run_started = time.perf_counter()
    parser = argparse.ArgumentParser(description="Chunked whisper.cpp transcription.")
    parser.add_argument("--input-video", required=True, type=Path)
    parser.add_argument("--first-audio", action="store_true", help="Use audio ordinal zero, matching Whisper C; omit only for legacy checkpoints.")
    parser.add_argument("--output-srt", required=True, type=Path)
    parser.add_argument("--raw-output-srt", required=True, type=Path)
    parser.add_argument("--output-txt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--whisper", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--chunk-seconds", type=int, default=600)
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.chunk_seconds <= 0:
        raise ValueError("--chunk-seconds must be positive")

    total_duration = ffprobe_duration(args.ffprobe, args.input_video)
    if args.duration_seconds > 0:
        total_duration = min(total_duration, float(args.duration_seconds))

    args.work_dir.mkdir(parents=True, exist_ok=True)
    ascii_model = args.work_dir / args.model.name
    hardlink_or_copy(args.model, ascii_model)

    merged_blocks: list[Block] = []
    txt_parts: list[str] = []
    chunk_count = int(math.ceil(total_duration / args.chunk_seconds))
    chunks_report: list[dict] = []

    for chunk_index in range(chunk_count):
        chunk_started = time.perf_counter()
        extract_seconds = 0.0
        transcribe_seconds = 0.0
        start = chunk_index * args.chunk_seconds
        length = min(args.chunk_seconds, total_duration - start)
        if length <= 0:
            continue
        chunk_stem = f"chunk_{chunk_index + 1:04d}_{int(start):06d}s"
        wav_path = args.work_dir / f"{chunk_stem}.wav"
        out_base = args.work_dir / chunk_stem
        srt_path = args.work_dir / f"{chunk_stem}.srt"
        txt_path = args.work_dir / f"{chunk_stem}.txt"
        incomplete_path = args.work_dir / f"{chunk_stem}.incomplete"
        chunk_complete = (
            not incomplete_path.exists()
            and srt_path.exists()
            and srt_path.stat().st_size > 0
            and txt_path.exists()
            and txt_path.stat().st_size > 0
        )
        if chunk_complete:
            print(f"[resume] using completed {chunk_stem}", flush=True)
        else:
            # A stop may leave nonempty SRT/TXT files. Only a successful Whisper
            # return clears this marker, so resume never accepts a partial pair.
            incomplete_path.write_text("transcription in progress\n", encoding="utf-8")
            for old_path in (wav_path, srt_path, txt_path):
                if old_path.exists():
                    old_path.unlink()

            extract_started = time.perf_counter()
            run_command(
                [
                    str(args.ffmpeg),
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{length:.3f}",
                    "-i",
                    str(args.input_video),
                    *(["-map", "0:a:0"] if args.first_audio else []),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(wav_path),
                ]
            )
            extract_seconds = time.perf_counter() - extract_started
            transcribe_started = time.perf_counter()
            run_command(
                [
                    str(args.whisper),
                    "-m",
                    str(ascii_model),
                    "-f",
                    str(wav_path),
                    "-l",
                    "ja",
                    "-osrt",
                    "-otxt",
                    "-of",
                    str(out_base),
                    "-t",
                    str(args.threads),
                    "-bo",
                    "5",
                    "-bs",
                    "5",
                    "-mc",
                    "0",
                    "-np",
                    "--prompt",
                    "Japanese spoken dialogue. Keep proper nouns and short utterances accurate.",
                ]
            )

            transcribe_seconds = time.perf_counter() - transcribe_started
            incomplete_path.unlink()

        blocks = parse_srt(srt_path, start)
        merged_blocks.extend(blocks)
        if txt_path.exists():
            txt_parts.append(f"\n\n===== {chunk_stem} start={start:.3f}s =====\n")
            txt_parts.append(txt_path.read_text(encoding="utf-8-sig", errors="replace"))
        chunks_report.append(
            {
                "chunk": chunk_index + 1,
                "start": start,
                "duration": length,
                "blocks": len(blocks),
                "srt": str(srt_path),
                "reused": chunk_complete,
                "audio_extract_seconds": extract_seconds,
                "transcribe_seconds": transcribe_seconds,
                "current_attempt_wall_seconds": time.perf_counter() - chunk_started,
            }
        )

    merged_blocks.sort(key=lambda block: (block.start, block.end))
    write_srt(args.raw_output_srt, merged_blocks)
    write_srt(args.output_srt, merged_blocks)
    args.output_txt.parent.mkdir(parents=True, exist_ok=True)
    args.output_txt.write_text("".join(txt_parts).strip() + "\n", encoding="utf-8")

    report = {
        "input_video": str(args.input_video),
        "output_srt": str(args.output_srt),
        "raw_output_srt": str(args.raw_output_srt),
        "duration": total_duration,
        "chunk_seconds": args.chunk_seconds,
        "chunks": chunks_report,
        "blocks": len(merged_blocks),
        "model": str(args.model.resolve()),
        "whisper": str(args.whisper.resolve()),
        "current_attempt_wall_seconds": time.perf_counter() - run_started,
        "audio_extract_seconds": sum(chunk["audio_extract_seconds"] for chunk in chunks_report),
        "transcribe_seconds": sum(chunk["transcribe_seconds"] for chunk in chunks_report),
        "reused_chunks": sum(chunk["reused"] for chunk in chunks_report),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
