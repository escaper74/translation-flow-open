"""Conservative opening-only timing correction and a separate review CLI.

New ASR jobs use the same locally accepted rule before handing off to translation.
Completed/legacy jobs are unchanged; the CLI still writes independent candidates.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unicodedata
import wave

from .srt import SubtitleCue, parse_srt, validate_timeline, write_srt

OPENING_TIMING_POLICY = "leading_silence_v1"


def vad_command(cli: Path, model: Path, wav: Path) -> list[str]:
    # Standalone detector defaults to CPU; do not enable --use-gpu.
    return [str(cli), "--vad-model", str(model), "--file", str(wav),
            "--vad-threshold", "0.35", "--vad-min-speech-duration-ms", "100",
            "--vad-min-silence-duration-ms", "500", "--vad-speech-pad-ms", "250", "--no-prints"]


def prepare_opening_audio(first_chunk: Path, model: Path) -> tuple[Path, Path, int]:
    """Reuse A's PCM on its ASCII path; no media/ASR rerun or source edits."""
    directory = first_chunk.parent
    if not str(directory).isascii():
        raise ValueError("Opening detector needs the existing ASCII ASR work path")
    local_model = directory / "opening-silero-v6.2.0.bin"
    model_data = model.read_bytes()
    if local_model.exists():
        if local_model.read_bytes() != model_data:
            raise ValueError("Opening detector model differs from this checkpoint")
    else:
        with local_model.open("xb") as output:
            output.write(model_data)
    opening = directory / "opening-timing.wav"
    with wave.open(str(first_chunk), "rb") as original:
        params = original.getparams()
        frames = original.readframes(min(original.getnframes(), original.getframerate() * 60))
    with wave.open(str(opening), "wb") as output:
        output.setparams(params)
        output.writeframes(frames)
    duration_ms = round(len(frames) / (params.nchannels * params.sampwidth) / params.framerate * 1000)
    return opening, local_model, duration_ms


def normalized_text(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c)[0] not in "PZS")


def speech_from_log(log: str, duration_ms: int) -> list[tuple[int, int]]:
    # whisper.cpp v1.9.0 VAD getters return centiseconds, not milliseconds.
    # https://github.com/ggml-org/whisper.cpp/blob/v1.9.0/src/whisper.cpp
    matches = re.findall(r"Speech segment (\d+): start = ([\d.]+), end = ([\d.]+)", log)
    segments = []
    for position, (index, start, end) in enumerate(matches):
        lo, hi = round(float(start) * 10), round(float(end) * 10)
        if int(index) != position or lo < 0 or hi <= lo or hi > duration_ms + 100:
            raise ValueError("Invalid VAD interval or unexpected timestamp units")
        if segments and lo < segments[-1][1]:
            raise ValueError("Overlapping/out-of-order VAD intervals")
        segments.append((lo, hi))
    count = re.search(r"Detected (\d+) speech segments:", log)
    if count is None or int(count[1]) != len(segments):
        raise ValueError("VAD did not return a complete segment result")
    return segments


def propose_opening(
    a: list[SubtitleCue], c: list[SubtitleCue], speech: list[tuple[int, int]],
) -> tuple[list[SubtitleCue], dict]:
    """Suggest a contiguous misplaced opening prefix; never rewrite text.

    These bounds select review candidates, not general accuracy thresholds.
    Missing/ambiguous evidence returns the untouched input, not a job failure.
    """
    unchanged = list(a)

    def skip(reason):
        return unchanged, {"status": "no_candidate", "reason": reason, "changes": []}

    if not a or not c or not speech:
        return skip("missing_speech_or_subtitle_evidence")
    speech_start = speech[0][0]
    if a[0].start_ms > 2000 or not 2000 < speech_start <= 30000:
        return skip("not_a_leading_silence_case")
    prefix = []
    for cue in a:
        if cue.end_ms > speech_start:
            break
        prefix.append(cue)
    if not 2 <= len(prefix) <= 8 or len(prefix) == len(a):
        return skip("no_bounded_opening_prefix")
    boundary = a[len(prefix)].start_ms
    window = [q for q in c if speech_start - 500 <= q.start_ms < min(boundary, 60000)
              and q.end_ms <= boundary]
    chosen: list[SubtitleCue] = []
    rows = []
    for cue in prefix:
        text = normalized_text(cue.text)
        if len(text) < 2:
            return skip("opening_text_too_short_to_match")
        ranked = sorted(
            [(SequenceMatcher(None, text, normalized_text(q.text), autojunk=False).ratio(), q)
             for q in window], key=lambda pair: pair[0], reverse=True,
        )
        if not ranked:
            return skip("no_c_match")
        score, match = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0
        if score < .65 or score - runner_up < .20:
            return skip("ambiguous_or_weak_c_match")
        if chosen and (match.index <= chosen[-1].index or match.start_ms < chosen[-1].end_ms):
            return skip("c_matches_not_in_order")
        if match.start_ms - cue.start_ms < 2000:
            return skip("not_a_consistent_early_opening")
        overlap = sum(max(0, min(match.end_ms, hi) - max(match.start_ms, lo))
                      for lo, hi in speech)
        if overlap / (match.end_ms - match.start_ms) < .5:
            return skip("c_match_not_supported_by_speech")
        chosen.append(match)
        rows.append({"id": cue.index, "old_start_ms": cue.start_ms, "old_end_ms": cue.end_ms,
                     "new_start_ms": match.start_ms, "new_end_ms": match.end_ms,
                     "c_id": match.index, "text_similarity": round(score, 4),
                     "match_margin": round(score - runner_up, 4),
                     "speech_overlap_ratio": round(overlap / (match.end_ms - match.start_ms), 4)})
    shifts = [q.start_ms - p.start_ms for p, q in zip(prefix, chosen)]
    if max(shifts) - min(shifts) > 2000:
        return skip("opening_shifts_disagree")
    if sum(row["text_similarity"] >= .8 for row in rows) < 2:
        return skip("insufficient_strong_text_anchors")
    corrected = [replace(cue, start_ms=match.start_ms, end_ms=match.end_ms)
                 for cue, match in zip(prefix, chosen)] + a[len(prefix):]
    return corrected, {"status": "candidate_for_human_review", "changes": rows,
                       "first_speech_ms": speech_start, "unchanged_from_id": a[len(prefix)].index,
                       "unchanged_from_ms": boundary, "human_review_status": "pending"}


def copy_timing(original: list[SubtitleCue], corrected: list[SubtitleCue],
                target: list[SubtitleCue]) -> list[SubtitleCue]:
    validate_timeline(original, target)
    if len(corrected) != len(original) or any(
        x.index != y.index or x.text != y.text for x, y in zip(original, corrected)
    ):
        raise ValueError("Timing correction changed the backbone content or IDs")
    return [replace(t, start_ms=q.start_ms, end_ms=q.end_ms) for t, q in zip(target, corrected)]


def create_candidate(a_srt: Path, c_srt: Path, japanese_srt: Path, chinese_srt: Path,
                     speech: list[tuple[int, int]], output_dir: Path) -> dict:
    inputs = [a_srt, c_srt, japanese_srt, chinese_srt]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    a, c, ja, zh = (parse_srt(p) for p in inputs)
    corrected, report = propose_opening(a, c, speech)
    ja_out, zh_out = copy_timing(a, corrected, ja), copy_timing(a, corrected, zh)
    # Refuse reuse of any directory, including a formal checkpoint directory.
    output_dir.mkdir(parents=True, exist_ok=False)
    report.update(kind="opening_timing_candidate", production_enabled=False,
                  input_sha256=before, speech_segments_ms=speech, total_cues=len(a))
    if report["status"] == "candidate_for_human_review":
        outputs = {}
        for name, original, revised in (("japanese", ja, ja_out), ("chinese", zh, zh_out)):
            path = output_dir / f"candidate.{name}.srt"
            write_srt(path, revised)
            readback = parse_srt(path)
            if readback != revised or any(x.text != y.text for x, y in zip(original, readback)):
                raise ValueError("Candidate did not preserve subtitle content")
            changed_ids = {row["id"] for row in report["changes"]}
            if any(x != y for x, y in zip(original, readback) if x.index not in changed_ids):
                raise ValueError("Candidate changed the unaffected remainder")
            outputs[name] = str(path.resolve())
        report["outputs"] = outputs
        report["all_text_preserved"] = True
        report["remaining_cues_unchanged"] = len(a) - len(report["changes"])
    report["inputs_unchanged"] = before == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    if not report["inputs_unchanged"]:
        raise ValueError("An input changed during candidate generation")
    (output_dir / "timing-candidate.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("a-srt", "c-srt", "japanese-srt", "chinese-srt", "vad-model",
                 "vad-cli", "opening-wav", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise ValueError("Use a new candidate output directory")
    with wave.open(str(args.opening_wav), "rb") as wav:
        duration_ms = round(wav.getnframes() / wav.getframerate() * 1000)
    if not 0 < duration_ms <= 120000:
        raise ValueError("Use an opening WAV of at most 120 seconds")
    command = vad_command(args.vad_cli, args.vad_model, args.opening_wav)
    # The standalone detector defaults to CPU; never pass --use-gpu.
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log = result.stdout + "\n" + result.stderr
    if result.returncode:
        raise RuntimeError("Opening VAD failed; no candidate generated")
    speech = speech_from_log(log, duration_ms)
    report = create_candidate(args.a_srt, args.c_srt, args.japanese_srt,
                              args.chinese_srt, speech, args.output_dir)
    (args.output_dir / "vad.log").write_text(log, encoding="utf-8")
    print(json.dumps({"status": report["status"], "changed_cues": len(report["changes"]),
                      "human_review_status": report.get("human_review_status"),
                      "inputs_unchanged": report["inputs_unchanged"]}))


if __name__ == "__main__":
    main()
