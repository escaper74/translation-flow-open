from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .media_input import check_media_input
from .cancellation import check_cancelled, run_asr_process
from .opening_timing import (
    OPENING_TIMING_POLICY, copy_timing, prepare_opening_audio, propose_opening,
    speech_from_log, vad_command,
)
from .translation.codex_provider import TranslationCancelled
from .srt import (
    SubtitleCue,
    parse_srt,
    validate_positive_durations,
    validate_timeline,
    write_srt,
)


Emit = Callable[..., Any] | Any
ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _emit(target: Emit | None, event: str, **payload: Any) -> None:
    if target is None:
        return
    method = getattr(target, "emit", None)
    if callable(method):
        method(event, **payload)
    elif callable(target):
        target(event, **payload)


def _json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_completed_asr(source: Path, job_root: Path, emit: Emit | None = None, *,
                       a_model_id: str = "large-v3") -> tuple[Path, dict[str, Any]]:
    """The existing completed-ASR checks, without requiring or launching tools."""
    source, job_root = source.resolve(), job_root.resolve()
    a_cues = parse_srt(job_root / "A" / "A.ja.srt")
    c_cues = parse_srt(job_root / "C" / "C.ja.srt")
    fusion_srt = job_root / "fusion" / "fusion.ja.srt"
    fused_cues = parse_srt(fusion_srt)
    if not a_cues or not c_cues or not fused_cues:
        raise ValueError("resumed ASR produced no subtitle cues")
    validate_timeline(a_cues, fused_cues)
    report = json.loads((job_root / "asr.report.json").read_text(encoding="utf-8-sig"))
    stat = source.stat()
    if (not isinstance(report, dict) or report.get("status") != "complete"
            or Path(str(report.get("source", ""))).resolve() != source
            or report.get("source_size_bytes") != stat.st_size
            or report.get("source_mtime_ns") != stat.st_mtime_ns
            or report.get("a_model", "large-v3") != a_model_id
            or report.get("timeline_preserved_from_a") is not True
            or report.get("a_cues") != len(a_cues)
            or report.get("c_cues") != len(c_cues)
            or report.get("fusion_cues") != len(fused_cues)):
        raise ValueError("completed ASR checkpoint does not match this source or timeline")
    for stage, count, overall in (
        ("asr_a", len(a_cues), 25), ("asr_c", len(c_cues), 42), ("asr_fusion", len(fused_cues), 47),
    ):
        _emit(emit, "stage_completed", stage=stage, cues=count, resumed=True, overall_percent=overall)
    return fusion_srt, report


def _ascii_a_work_dir(job_root: Path, whisper_model: Path) -> Path:
    configured = os.environ.get("TRANSLATION_FLOW_ASCII_WORK_ROOT", "").strip()
    if configured:
        base = Path(configured)
    else:
        # Keep the work directory on the model's volume when an ASCII-only
        # ancestor is available. The A transcriber can then hard-link the
        # multi-GB model instead of copying it across drives for every job.
        base = None
        for parent in (whisper_model.parent, *whisper_model.parents):
            if str(parent).isascii():
                base = parent / "escaper_translation_flow_work"
                break
        if base is None:
            base = Path(tempfile.gettempdir()) / "escaper_translation_flow"
    identity = hashlib.sha256(str(job_root).encode("utf-8")).hexdigest()[:24]
    work_dir = (base / identity / "A_chunks").resolve()
    if not str(work_dir).isascii():
        raise ValueError(
            "Whisper A requires an ASCII work directory; set "
            "TRANSLATION_FLOW_ASCII_WORK_ROOT to an ASCII-only local path"
        )
    return work_dir


@dataclass(frozen=True)
class ToolPaths:
    """Explicit local tool locations used by the two independent ASR processes."""

    python: Path
    faster_python: Path
    ffmpeg: Path
    ffprobe: Path
    whisper: Path
    whisper_model: Path
    faster_model: Path
    transcribe_a: Path
    clean_srt: Path
    validate_quality: Path
    transcribe_c: Path
    validate_structure: Path
    cuda_runtime_dirs: tuple[Path, ...] = ()
    a_model_id: str = "large-v3"
    vad_cli: Path | None = None
    vad_model: Path | None = None

    @classmethod
    def from_root(
        cls,
        tool_root: str | Path,
        faster_root: str | Path,
        *,
        python: str | Path | None = None,
        a_model_id: str = "large-v3",
    ) -> "ToolPaths":
        from .contracts import SUPPORTED_ASR_A_MODELS
        if a_model_id not in SUPPORTED_ASR_A_MODELS:
            raise ValueError("unsupported Whisper A model")
        tool_root = Path(tool_root)
        faster_root = Path(faster_root)
        repo_root = Path(__file__).resolve().parents[1]
        site_packages = (
            faster_root
            / "venvs"
            / "faster-whisper-1.2.1"
            / "Lib"
            / "site-packages"
        )
        return cls(
            python=Path(python) if python else Path(sys.executable),
            faster_python=faster_root
            / "venvs"
            / "faster-whisper-1.2.1"
            / "Scripts"
            / "python.exe",
            ffmpeg=tool_root
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
            / "ffmpeg.exe",
            ffprobe=tool_root
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
            / "ffprobe.exe",
            whisper=tool_root
            / "whisper-cublas-12.4.0-bin-x64"
            / "Release"
            / "whisper-cli.exe",
            whisper_model=tool_root
            / "LLPlayer-v0.3.0"
            / "whispermodels"
            / f"ggml-{a_model_id}.bin",
            a_model_id=a_model_id,
            vad_cli=tool_root / "whisper-cublas-12.4.0-bin-x64" / "Release" / "whisper-vad-speech-segments.exe",
            vad_model=tool_root / "whisper-vad" / "ggml-silero-v6.2.0.bin",
            faster_model=faster_root / "models" / "faster-whisper-large-v3",
            transcribe_a=repo_root / "workflow" / "scripts" / "transcribe_whisper_chunked.py",
            clean_srt=repo_root / "workflow" / "scripts" / "clean_whisper_srt.py",
            validate_quality=repo_root / "workflow" / "scripts" / "validate_srt_quality.py",
            transcribe_c=repo_root
            / "workflow"
            / "scripts"
            / "asr_fusion"
            / "transcribe_faster_whisper.py",
            validate_structure=repo_root
            / "workflow"
            / "scripts"
            / "asr_fusion"
            / "validate_asr_srt.py",
            cuda_runtime_dirs=tuple(
                site_packages / relative
                for relative in (
                    Path("nvidia/cublas/bin"),
                    Path("nvidia/cudnn/bin"),
                    Path("nvidia/cuda_nvrtc/bin"),
                )
            ),
        )

    def required_paths(self) -> tuple[Path, ...]:
        return (
            self.python,
            self.faster_python,
            self.ffmpeg,
            self.ffprobe,
            self.whisper,
            self.whisper_model,
            self.faster_model,
            self.transcribe_a,
            self.clean_srt,
            self.validate_quality,
            self.transcribe_c,
            self.validate_structure,
        )


@dataclass(frozen=True)
class FusionPolicy:
    min_containment: float = 0.65
    min_text_similarity: float = 0.48
    strong_containment: float = 0.82
    relaxed_text_similarity: float = 0.38
    min_character_f1: float = 0.50


@dataclass(frozen=True)
class FusionMatch:
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


def _normalized(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S"))
    )


def _text_scores(left: str, right: str) -> tuple[float, float, float]:
    left = _normalized(left)
    right = _normalized(right)
    if not left or not right:
        return 0.0, 0.0, 0.0
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    common = sum((Counter(left) & Counter(right)).values())
    precision = common / len(right)
    recall = common / len(left)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return sequence, f1, max(sequence, f1)


def _overlap(left: SubtitleCue, right: SubtitleCue) -> int:
    return max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))


def _significant(left: SubtitleCue, right: SubtitleCue) -> bool:
    overlap = _overlap(left, right)
    shorter = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return shorter > 0 and overlap / shorter >= 0.35


def conservative_fuse(
    backbone: Sequence[SubtitleCue],
    candidates: Sequence[SubtitleCue],
    *,
    policy: FusionPolicy = FusionPolicy(),
) -> tuple[list[SubtitleCue], list[FusionMatch]]:
    """Keep every A cue and replace text only for unique reliable A/C pairs."""

    validate_positive_durations(backbone, label="Whisper A backbone")
    validate_positive_durations(candidates, label="Whisper C candidates")
    by_a = {
        cue.index: [candidate for candidate in candidates if _significant(cue, candidate)]
        for cue in backbone
    }
    by_c = {
        cue.index: [source for source in backbone if _significant(source, cue)]
        for cue in candidates
    }
    matches: list[FusionMatch] = []
    candidate_by_index = {cue.index: cue for cue in candidates}

    for a_cue in backbone:
        for c_cue in by_a[a_cue.index]:
            overlap = _overlap(a_cue, c_cue)
            a_duration = a_cue.end_ms - a_cue.start_ms
            c_duration = c_cue.end_ms - c_cue.start_ms
            containment = overlap / min(a_duration, c_duration)
            duration_ratio = c_duration / a_duration
            length_ratio = len(_normalized(c_cue.text)) / max(
                len(_normalized(a_cue.text)), 1
            )
            sequence, character_f1, similarity = _text_scores(a_cue.text, c_cue.text)
            center_distance = abs(
                (a_cue.start_ms + a_cue.end_ms)
                - (c_cue.start_ms + c_cue.end_ms)
            ) / 2
            center_score = max(
                0.0, 1.0 - center_distance / max(a_duration, c_duration, 1)
            )
            score = 0.50 * containment + 0.30 * similarity + 0.20 * center_score

            accepted = True
            reason = "accepted_unique_one_to_one"
            if len(by_a[a_cue.index]) != 1 or len(by_c[c_cue.index]) != 1:
                accepted, reason = False, "ambiguous_segmentation"
            elif containment < policy.min_containment:
                accepted, reason = False, "weak_time_containment"
            elif not 0.55 <= duration_ratio <= 1.80:
                accepted, reason = False, "duration_ratio_out_of_range"
            elif not 0.50 <= length_ratio <= 2.00:
                accepted, reason = False, "text_length_ratio_out_of_range"
            elif not (
                similarity >= policy.min_text_similarity
                or (
                    containment >= policy.strong_containment
                    and similarity >= policy.relaxed_text_similarity
                    and character_f1 >= policy.min_character_f1
                )
            ):
                accepted, reason = False, "weak_japanese_text_agreement"
            matches.append(
                FusionMatch(
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

    accepted = {match.a_index: match for match in matches if match.accepted}
    fused = []
    for cue in backbone:
        match = accepted.get(cue.index)
        text = candidate_by_index[match.c_index].text if match else cue.text
        fused.append(SubtitleCue(cue.index, cue.start_ms, cue.end_ms, text.strip()))
    validate_timeline(list(backbone), fused)
    return fused, matches


class AsrPipeline:
    def __init__(
        self,
        tools: ToolPaths,
        *,
        threads: int = 8,
        chunk_seconds: int = 600,
        process_runner: ProcessRunner = subprocess.run,
        fusion_policy: FusionPolicy = FusionPolicy(),
    ) -> None:
        self.tools = tools
        self.threads = threads
        self.chunk_seconds = chunk_seconds
        self.process_runner = process_runner
        self.fusion_policy = fusion_policy
        self.cancelled: Callable[[], bool] = lambda: False

    def _opening_timing(self, a_cues, c_cues, a_work_dir, emit=None):
        """Optional local correction. Uncertain evidence never blocks translation."""
        if not a_cues or not c_cues or a_cues[0].start_ms > 2000 or c_cues[0].start_ms <= 2000:
            return a_cues, {"status": "not_applicable", "changes": [], "policy": OPENING_TIMING_POLICY}
        try:
            if not all(p is not None and p.is_file() for p in (self.tools.vad_cli, self.tools.vad_model)):
                raise FileNotFoundError("local VAD component unavailable")
            wav, model, duration_ms = prepare_opening_audio(
                a_work_dir / "chunk_0001_000000s.wav", self.tools.vad_model)
            result = self._run_process(vad_command(self.tools.vad_cli, model, wav), "Opening timing VAD")
            speech = speech_from_log(result.stdout + "\n" + result.stderr, duration_ms)
            corrected, timing = propose_opening(a_cues, c_cues, speech)
            timing.update(policy=OPENING_TIMING_POLICY, speech_segments_ms=[list(pair) for pair in speech])
            if timing["changes"]:
                timing["status"] = "applied"
                _emit(emit, "stage_progress", stage="asr_fusion", overall_percent=44,
                      message=f"已校正开头 {len(timing['changes'])} 条字幕时间，文字和后续时间未改。")
            return corrected, timing
        except TranslationCancelled:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            # Keep this an optional improvement, not another full-job Gate.
            _emit(emit, "warning", stage="asr_fusion", code="OPENING_TIMING_SKIPPED",
                  message="开头校时组件未能完成，已保留原字幕时间并继续；详情见 ASR 报告。")
            return a_cues, {"status": "skipped", "changes": [], "policy": OPENING_TIMING_POLICY,
                            "reason": type(exc).__name__, "detail": str(exc)[-1000:]}

    def _run_process(
        self,
        args: list[str],
        label: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        check_cancelled(self.cancelled)
        if self.process_runner is subprocess.run:
            result = run_asr_process(args, cancelled=self.cancelled,
                                     env=dict(env) if env is not None else None)
        else:
            result = self.process_runner(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(env) if env is not None else None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        check_cancelled(self.cancelled)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise RuntimeError(f"{label} failed with exit code {result.returncode}: {detail}")
        return result

    def _run_report_check(
        self,
        args: list[str],
        label: str,
        report_path: Path,
    ) -> list[str]:
        result = self.process_runner(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not report_path.is_file():
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise RuntimeError(f"{label} wrote no report (exit {result.returncode}): {detail}")
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        failures = [str(item) for item in payload.get("failures", [])]
        warnings = [str(item) for item in payload.get("warnings", [])]
        if result.returncode == 0 and not failures:
            return warnings
        detail = (result.stderr or result.stdout or "").strip()[-1000:]
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}: "
            + ("; ".join(failures) if failures else detail)
        )

    def _duration(self, source: Path) -> float:
        result = self._run_process(
            [
                str(self.tools.ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            "source duration probe",
        )
        try:
            duration = float(result.stdout.strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("FFprobe did not return a usable source duration") from exc
        if duration <= 0:
            raise RuntimeError("source duration must be positive")
        return duration

    @staticmethod
    def _require_absent(paths: Sequence[Path]) -> None:
        existing = [str(path) for path in paths if path.exists()]
        if existing:
            raise FileExistsError("refusing to overwrite ASR artifacts: " + ", ".join(existing))

    @staticmethod
    def _assert_cues(path: Path, label: str) -> list[SubtitleCue]:
        cues = parse_srt(path)
        if not cues:
            raise ValueError(f"{label} produced no subtitle cues")
        return cues

    def _gate(
        self, srt: Path, report_prefix: Path, duration: float, *, c: bool
    ) -> list[str]:
        quality_report = report_prefix.with_suffix(".quality.json")
        structure_report = report_prefix.with_suffix(".structure.json")
        self._run_process(
            [
                str(self.tools.python),
                str(self.tools.validate_quality),
                "--input",
                str(srt),
                "--report",
                str(quality_report),
            ],
            f"{report_prefix.name} quality gate",
        )
        args = [
            str(self.tools.python),
            str(self.tools.validate_structure),
            "--input",
            str(srt),
            "--source-duration",
            str(duration),
            "--report",
            str(structure_report),
        ]
        if c:
            args.extend(["--max-lines", "2", "--max-line-chars", "16"])
        return self._run_report_check(
            args,
            f"{report_prefix.name} structure gate",
            structure_report,
        )

    def run(
        self,
        source: str | Path,
        job_root: str | Path,
        emit: Emit | None = None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        self.cancelled = cancelled or (lambda: False)
        check_cancelled(self.cancelled)
        source = Path(source).resolve()
        job_root = Path(job_root).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        missing = [str(path) for path in self.tools.required_paths() if not path.exists()]
        if missing:
            raise FileNotFoundError("required ASR tools are missing: " + ", ".join(missing))
        if self.threads <= 0 or self.chunk_seconds <= 0:
            raise ValueError("threads and chunk_seconds must be positive")

        source_stat = source.stat()
        source_identity = {
            "source": str(source),
            "source_size_bytes": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
        }
        a_dir, c_dir, fusion_dir = (job_root / name for name in ("A", "C", "fusion"))
        a_raw, a_transcribed, a_srt = (
            a_dir / "A.raw.srt",
            a_dir / "A.transcribed.srt",
            a_dir / "A.ja.srt",
        )
        c_raw, c_srt = c_dir / "C.raw.srt", c_dir / "C.ja.srt"
        fusion_srt = fusion_dir / "fusion.ja.srt"
        report_path = job_root / "asr.report.json"
        resume_outputs = (a_srt, c_srt, fusion_srt, report_path)
        if all(path.is_file() for path in resume_outputs):
            return load_completed_asr(source, job_root, emit, a_model_id=self.tools.a_model_id)
        source_marker = job_root / "asr.input.json"
        occupied_stage_dirs = [
            str(directory)
            for directory in (a_dir, c_dir, fusion_dir)
            if directory.exists() and any(directory.iterdir())
        ]
        if source_marker.is_file():
            marker = json.loads(source_marker.read_text(encoding="utf-8-sig"))
            first_audio = marker.get("asr_audio_selection") == "first"
            opening_enabled = marker.get("opening_timing_policy") == OPENING_TIMING_POLICY
            if (
                Path(str(marker.get("source", ""))).resolve() != source
                or marker.get("source_size_bytes") != source_identity["source_size_bytes"]
                or marker.get("source_mtime_ns") != source_identity["source_mtime_ns"]
                or marker.get("a_model", "large-v3") != self.tools.a_model_id
            ):
                raise ValueError("partial ASR checkpoint belongs to a different source")
        elif occupied_stage_dirs:
            raise FileExistsError(
                "refusing to mix old ASR stage artifacts: "
                + ", ".join(occupied_stage_dirs)
            )
        else:
            first_audio = True
            opening_enabled = True
            _json_write(source_marker, {**source_identity, "asr_audio_selection": "first",
                                       "a_model": self.tools.a_model_id,
                                       "opening_timing_policy": OPENING_TIMING_POLICY})
        # Raw/recognized A stays available even if its opening is calibrated.
        # Markers created before this feature retain the old timeline on resume.
        a_recognized = a_dir / "A.recognized.ja.srt" if opening_enabled else a_srt
        for directory in (a_dir, c_dir, fusion_dir):
            directory.mkdir(parents=True, exist_ok=True)

        _emit(emit, "stage_started", stage="media_input", overall_percent=1)
        media = check_media_input(source, job_root, self.tools.ffmpeg, self.tools.ffprobe,
                                  process_runner=self.process_runner, first_audio=first_audio)
        _json_write(job_root / "media-input.report.json", media)
        for warning in media["warnings"]:
            _emit(emit, "warning", stage="media_input", code="MEDIA_INPUT_SELECTION", message=warning)
        _emit(emit, "stage_completed", stage="media_input", overall_percent=2)
        duration = media["duration_seconds"]
        a_work_dir = _ascii_a_work_dir(job_root, self.tools.whisper_model)
        _emit(
            emit,
            "stage_started",
            stage="asr_a",
            resumed=a_work_dir.is_dir() and any(a_work_dir.glob("chunk_*.srt")),
            overall_percent=3,
        )
        self._run_process(
            [
                str(self.tools.python),
                str(self.tools.transcribe_a),
                "--input-video",
                str(source),
                *(["--first-audio"] if first_audio else []),
                "--output-srt",
                str(a_transcribed),
                "--raw-output-srt",
                str(a_raw),
                "--output-txt",
                str(a_dir / "A.txt"),
                "--work-dir",
                str(a_work_dir),
                "--ffmpeg",
                str(self.tools.ffmpeg),
                "--ffprobe",
                str(self.tools.ffprobe),
                "--whisper",
                str(self.tools.whisper),
                "--model",
                str(self.tools.whisper_model),
                "--threads",
                str(self.threads),
                "--chunk-seconds",
                str(self.chunk_seconds),
                "--report",
                str(a_dir / "A.transcription.json"),
            ],
            "Whisper A transcription",
        )
        self._run_process(
            [
                str(self.tools.python),
                str(self.tools.clean_srt),
                "--input",
                str(a_raw),
                "--output",
                str(a_recognized),
                "--report",
                str(a_dir / "A.clean.json"),
            ],
            "Whisper A cleanup",
        )
        a_cues = self._assert_cues(a_recognized, "Whisper A")
        a_structure_warnings = self._gate(a_recognized, a_dir / "A", duration, c=False)
        for warning in a_structure_warnings:
            _emit(
                emit,
                "warning",
                stage="asr_a",
                code="ASR_PRESENTATION_WARNING",
                message=warning,
            )
        _emit(
            emit,
            "stage_completed",
            stage="asr_a",
            cues=len(a_cues),
            overall_percent=25,
        )
        _emit(emit, "resource_status", resource="gpu", state="released", after_stage="asr_a")

        # A and its whisper child have exited before C starts.
        _emit(
            emit,
            "stage_started",
            stage="asr_c",
            resumed=False,
            overall_percent=26,
        )
        c_env = os.environ.copy()
        runtime_dirs = [path for path in self.tools.cuda_runtime_dirs if path.is_dir()]
        if runtime_dirs:
            c_env["PATH"] = os.pathsep.join(map(str, runtime_dirs)) + os.pathsep + c_env.get("PATH", "")
        c_env.update({"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
        self._run_process(
            [
                str(self.tools.faster_python),
                str(self.tools.transcribe_c),
                "--input",
                str(source),
                "--model",
                str(self.tools.faster_model),
                "--raw-json",
                str(c_dir / "C.words.json"),
                "--model-srt",
                str(c_dir / "C.model-segments.srt"),
                "--output-srt",
                str(c_raw),
                "--output-txt",
                str(c_dir / "C.txt"),
                "--report",
                str(c_dir / "C.transcription.json"),
                "--device",
                "cuda",
                "--compute-type",
                "float16",
                "--beam-size",
                "5",
                "--vad-threshold",
                "0.35",
                "--min-speech-ms",
                "100",
                "--min-silence-ms",
                "500",
                "--speech-pad-ms",
                "250",
                "--max-chars",
                "28",
                "--max-line-chars",
                "16",
                "--max-duration",
                "6.0",
                "--max-gap",
                "0.65",
            ],
            "Whisper C transcription",
            env=c_env,
        )
        self._run_process(
            [
                str(self.tools.python),
                str(self.tools.clean_srt),
                "--input",
                str(c_raw),
                "--output",
                str(c_srt),
                "--report",
                str(c_dir / "C.clean.json"),
            ],
            "Whisper C cleanup",
        )
        c_cues = self._assert_cues(c_srt, "Whisper C")
        c_structure_warnings = self._gate(c_srt, c_dir / "C", duration, c=True)
        for warning in c_structure_warnings:
            _emit(
                emit,
                "warning",
                stage="asr_c",
                code="ASR_PRESENTATION_WARNING",
                message=warning,
            )
        _emit(
            emit,
            "stage_completed",
            stage="asr_c",
            cues=len(c_cues),
            overall_percent=42,
        )
        _emit(emit, "resource_status", resource="gpu", state="released", after_stage="asr_c")

        _emit(emit, "stage_started", stage="asr_fusion", overall_percent=43)
        fused, matches = conservative_fuse(a_cues, c_cues, policy=self.fusion_policy)
        opening_timing = {"status": "legacy_unchanged", "changes": []}
        if opening_enabled:
            calibrated_a, opening_timing = self._opening_timing(a_cues, c_cues, a_work_dir, emit)
            # Fuse against the original timestamps first so calibration cannot
            # alter existing content decisions. Only transfer the changed times.
            fused = copy_timing(a_cues, calibrated_a, fused)
            a_cues = calibrated_a
            write_srt(a_srt, a_cues)
        _json_write(job_root / "opening-timing.report.json", opening_timing)
        write_srt(fusion_srt, fused)
        reparsed = parse_srt(fusion_srt)
        validate_timeline(a_cues, reparsed)
        fusion_structure_warnings = self._gate(
            fusion_srt, fusion_dir / "fusion", duration, c=False
        )
        for warning in fusion_structure_warnings:
            _emit(
                emit,
                "warning",
                stage="asr_fusion",
                code="ASR_PRESENTATION_WARNING",
                message=warning,
            )
        accepted = [match for match in matches if match.accepted]
        rejection_counts = Counter(match.reason for match in matches if not match.accepted)
        report: dict[str, Any] = {
            "status": "complete",
            "source": str(source),
            "source_size_bytes": source_identity["source_size_bytes"],
            "source_mtime_ns": source_identity["source_mtime_ns"],
            "source_duration_seconds": duration,
            "a_model": self.tools.a_model_id,
            "a_cues": len(a_cues),
            "c_cues": len(c_cues),
            "fusion_cues": len(reparsed),
            "c_replacements": len(accepted),
            "a_backbone_cues": len(a_cues) - len(accepted),
            "timeline_preserved_from_a": True,
            "a_recognized_srt": str(a_recognized),
            "opening_timing": opening_timing,
            "structure_warnings": {
                "A": a_structure_warnings,
                "C": c_structure_warnings,
                "fusion": fusion_structure_warnings,
            },
            "fusion_srt": str(fusion_srt),
            "a_chunk_work_dir": str(a_work_dir),
            "report_path": str(report_path),
            "policy": asdict(self.fusion_policy),
            "rejection_reasons": dict(sorted(rejection_counts.items())),
            "accepted_matches": [asdict(match) for match in accepted],
        }
        _json_write(report_path, report)
        _emit(
            emit,
            "stage_completed",
            stage="asr_fusion",
            cues=len(reparsed),
            replacements=len(accepted),
            overall_percent=47,
        )
        return fusion_srt, report
