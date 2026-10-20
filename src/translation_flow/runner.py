from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from .asr import AsrPipeline, ToolPaths, load_completed_asr
from .cancellation import CancellableProvider, StopAwareEvents, check_cancelled
from .contracts import ApiProviderRequest, JobRequest
from .events import EventEmitter
from .packaging import MkvSoftSubtitlePackager, validate_completed_mkv
from .runtime import DEFAULT_TOOL_ROOT, tool_root as resolve_tool_root
from .srt import SubtitleCue, parse_srt, subtitle_digest, validate_timeline
from .translation import create_deepseek_provider, run_translation
from .translation.api_providers import create_api_provider


_SAFE_STEM_RE = re.compile(r"[^\w.-]+", flags=re.UNICODE)


def _safe_stem(value: str) -> str:
    normalized = _SAFE_STEM_RE.sub("-", value).strip("-._")
    return normalized[:80] or "video"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _review_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _review_cell(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def _render_subtitle_review(
    japanese: list[SubtitleCue], chinese: list[SubtitleCue]
) -> str:
    validate_timeline(japanese, chinese)
    lines = [
        "# 字幕人工检查表",
        "",
        "> 本文件只用于按时间检查日文与最终中文；正式字幕以同名 SRT 为准。",
        "",
        "| 时间 | 日文 | 最终中文 |",
        "|---|---|---|",
    ]
    for source, translated in zip(japanese, chinese):
        time_range = f"{_review_time(source.start_ms)}–{_review_time(source.end_ms)}"
        lines.append(
            f"| {time_range} | {_review_cell(source.text)} | {_review_cell(translated.text)} |"
        )
    return "\n".join(lines) + "\n"


def _write_review_once(path: Path, content: str) -> None:
    if path.is_file():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"字幕检查表已存在且内容不匹配：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".subtitle-review-{uuid.uuid4().hex[:12]}.tmp"
    try:
        temporary.write_text(content, encoding="utf-8")
        if path.exists():
            raise FileExistsError(f"拒绝覆盖已有字幕检查表：{path}")
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_api_translation(
    request: JobRequest, asr: dict[str, Any], japanese: list[SubtitleCue],
    chinese: list[SubtitleCue], report: dict[str, Any], output: Path,
) -> None:
    """Check the new API result against public configuration, without a Key."""
    stat = request.input_video.stat()
    validate_timeline(japanese, chinese)
    if (asr.get("status") != "complete"
            or Path(asr.get("source", "")).resolve() != request.input_video.resolve()
            or asr.get("source_size_bytes") != stat.st_size
            or asr.get("source_mtime_ns") != stat.st_mtime_ns
            or asr.get("fusion_cues") != len(japanese)):
        raise ValueError("ASR 检查点与本次源媒体不匹配")
    if (report.get("status") != "passed"
            or report.get("profile") != request.profile_id
            or report.get("provider") != request.provider.id
            or report.get("model") != request.provider.model
            or report.get("provider_identity") != request.provider.cache_identity
            or report.get("source_sha256") != subtitle_digest(japanese)
            or report.get("output_sha256") != subtitle_digest(chinese)
            or report.get("subtitle_count") != len(japanese)
            or report.get("translated_count") != len(chinese)
            or Path(report.get("output", "")).resolve() != output.resolve()):
        raise ValueError("已完成翻译检查点与本次供应商/端点/模型/上限或字幕不匹配")


class TranslationFlowRunner:
    def __init__(
        self,
        request: JobRequest,
        api_key: str = "",
        *,
        project_root: str | Path | None = None,
        tool_root: str | Path | None = None,
        python: str | Path | None = None,
        provider_factory: Callable[..., Any] = create_deepseek_provider,
        api_provider_factory: Callable[..., Any] = create_api_provider,
        asr_factory: Callable[..., Any] = AsrPipeline,
        packager_factory: Callable[..., Any] = MkvSoftSubtitlePackager,
        translator: Callable[..., dict[str, Any]] = run_translation,
        emitter: EventEmitter | None = None,
        on_asr_ready: Callable[[], None] | None = None,
        codex_provider_factory: Callable[..., Any] | None = None,
        source_preparer: Callable[..., Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.request = request
        self.api_key = api_key
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2])
        self.tool_root = resolve_tool_root(tool_root, project_root=self.project_root)
        self.python = Path(python or sys.executable)
        self.provider_factory = provider_factory
        self.api_provider_factory = api_provider_factory
        self.asr_factory = asr_factory
        self.packager_factory = packager_factory
        self.translator = translator
        self.on_asr_ready = on_asr_ready
        self.codex_provider_factory = codex_provider_factory
        self.source_preparer = source_preparer
        self.cancelled = cancelled or (lambda: False)
        self.job_root = (
            request.output_dir / ".translation-flow" / request.job_id
        ).resolve()
        self.events = emitter or EventEmitter(
            request.job_id, self.job_root / "events.jsonl"
        )

    def cancel_requested(self) -> bool:
        return self.cancelled() or (self.job_root / "cancel.requested").exists()

    def check_cancelled(self) -> None:
        check_cancelled(self.cancel_requested)

    def _translation_progress(self, item: dict[str, Any]) -> None:
        event = str(item.get("event", "translation_progress"))
        if item.get("stage") == "source_review":
            if event == "warning":
                self.events.emit("warning", stage="source_review", code=item.get("code", "SOURCE_REVIEW"),
                                 message=str(item.get("message", "")))
            elif event.startswith("batch_"):
                self.events.emit("stage_progress", stage="source_review", overall_percent=47,
                                 detail=f"识别疑点复核批次 {item.get('batch', '')}",
                                 resumed=item.get("resumed", False))
            return
        mapping = {
            "translation_started": ("context", 48, 48),
            "context_chunk_completed": ("context", 48, 55),
            "translation_lanes_planned": ("translate_initial", 55, 55),
            "translation_batch_started": ("translate_initial", 55, 68),
            "translation_batch_completed": ("translate_initial", 55, 68),
            "polish_batch_started": ("polish", 68, 78),
            "polish_batch_completed": ("polish", 68, 78),
            "kana_repair_started": ("kana_repair", 78, 78),
            "kana_repair_completed": ("kana_repair", 78, 81),
            "translation_completed": ("subtitle_validation", 82, 82),
        }
        stage, start, end = mapping.get(event, ("translate_initial", 55, 68))
        overall = start
        detail = event
        current = item.get("current")
        total = item.get("total")
        stage_percent = None
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            stage_percent = round(current * 100 / total, 1)
            overall = round(start + (end - start) * current / total, 1)
            detail = f"{event} {current}/{total}"
        self.events.emit(
            "stage_progress",
            stage=stage,
            stage_percent=stage_percent,
            overall_percent=overall,
            detail=detail,
        )

    def run(self) -> dict[str, Any]:
        os.environ.pop("SUBTITLE_API_KEY", None)
        if self.request.provider.id == "codex":
            from .codex_runner import run_codex_translation
            return run_codex_translation(self)
        api_config = self.request.provider if isinstance(self.request.provider, ApiProviderRequest) else None
        if not self.api_key and api_config is None:
            raise ValueError("缺少本次 DeepSeek API Key")
        self.request.output_dir.mkdir(parents=True, exist_ok=True)
        self.job_root.mkdir(parents=True, exist_ok=True)
        (self.job_root / "cancel.requested").unlink(missing_ok=True)
        self.check_cancelled()
        stem = _safe_stem(self.request.input_video.stem)
        profile = self.request.profile_id.replace("_", "-")
        run_label = _safe_stem(self.request.job_id)[:12]
        chinese_srt = self.request.output_dir / f"{stem}.{profile}.{run_label}.zh-CN.srt"
        subtitle_review = (
            self.request.output_dir
            / f"{stem}.{profile}.{run_label}.subtitle-review.md"
        )
        final_mkv = self.request.output_dir / f"{stem}.{profile}.{run_label}.zh-CN.mkv"
        translation_report = self.job_root / "translation.report.json"
        technical_report = self.job_root / "technical-validation.report.json"
        package_checkpoint = bool(api_config and final_mkv.is_file() and technical_report.is_file())
        if api_config:
            if final_mkv.exists() != technical_report.exists():
                raise FileExistsError("MKV 与技术报告不完整，保留现场并停止")
        else:
            for target in (final_mkv, technical_report):
                if target.exists():
                    raise FileExistsError(f"拒绝覆盖已有结果：{target}")
        translation_checkpoint = chinese_srt.is_file() and translation_report.is_file()
        if chinese_srt.exists() != translation_report.exists():
            raise FileExistsError("中文字幕与翻译报告不完整，拒绝混用残缺终态")
        if package_checkpoint and not translation_checkpoint:
            raise ValueError("已有 MKV 缺少正式翻译检查点，保留现场并停止")
        if api_config and not translation_checkpoint and not self.api_key.strip():
            raise ValueError(f"缺少本次 {api_config.id} API Key，且没有可复用的完整翻译检查点")

        self.events.emit(
            "job_started",
            profile_id=self.request.profile_id,
            input_video=str(self.request.input_video),
        )
        tools = ToolPaths.from_root(
            self.tool_root,
            self.tool_root / "asr-lab",
            python=self.python,
            a_model_id=self.request.asr_a_model,
        )
        asr_report_path = self.job_root / "asr" / "asr.report.json"
        asr_reused = bool(api_config and asr_report_path.is_file())
        if asr_reused:
            fused_srt, asr_report = load_completed_asr(
                self.request.input_video, self.job_root / "asr", self.events,
                a_model_id=self.request.asr_a_model,
            )
        else:
            if api_config and translation_checkpoint:
                raise ValueError("已有翻译缺少正式 ASR 检查点，保留现场并停止")
            missing_tools = [str(path) for path in tools.required_paths() if not path.exists()]
            if missing_tools:
                raise FileNotFoundError("正式流程所需本地工具缺失：" + ", ".join(missing_tools))
            fused_srt, asr_report = self.asr_factory(tools).run(
                self.request.input_video, self.job_root / "asr", emit=StopAwareEvents(self.events, self.cancel_requested),
                cancelled=self.cancel_requested,
            )
        # Validate the formal ASR handoff before creating or contacting an external
        # translation provider. Real AsrPipeline validates A and C independently;
        # this boundary independently protects fusion checkpoints and substitutes.
        fused_cues = parse_srt(fused_srt)
        self.check_cancelled()
        if api_config and asr_report.get("fusion_cues") != len(fused_cues):
            raise ValueError("ASR fusion 数量与正式报告不匹配")
        if self.on_asr_ready is not None and not (api_config and translation_checkpoint):
            self.on_asr_ready()
        if translation_checkpoint:
            chinese_cues = parse_srt(chinese_srt)
            validate_timeline(fused_cues, chinese_cues)
            report = json.loads(translation_report.read_text(encoding="utf-8-sig"))
            if api_config:
                _validate_api_translation(self.request, asr_report, fused_cues, chinese_cues, report, chinese_srt)
                if package_checkpoint:
                    validate_completed_mkv(final_mkv, technical_report, self.request.input_video, chinese_srt)
                if self.on_asr_ready is not None:
                    self.on_asr_ready()
            if (
                report.get("status") != "passed"
                or report.get("profile") != self.request.profile_id
                or report.get("provider") != self.request.provider.id
                or report.get("model") != self.request.provider.model
                or report.get("source_sha256") != subtitle_digest(fused_cues)
                or Path(str(report.get("output", ""))).resolve()
                != chinese_srt.resolve()
            ):
                raise ValueError("已完成翻译检查点与本次任务不匹配")
            self.events.emit(
                "stage_completed",
                stage="subtitle_validation",
                resumed=True,
                overall_percent=82,
            )
        else:
            if api_config:
                provider = self.api_provider_factory(
                    api_config.id, self.api_key, model=api_config.model,
                    endpoint=api_config.endpoint_profile, max_output_tokens=api_config.max_output_tokens,
                )
            else:
                provider = self.provider_factory(
                    self.api_key, model=self.request.provider.model, api_base="https://api.deepseek.com",
                )
            provider = CancellableProvider(provider, self.cancel_requested)
            self.events.emit("stage_started", stage="preflight", overall_percent=47)
            provider.check_connection()
            self.events.emit("stage_completed", stage="preflight", overall_percent=48)
            report = self.translator(
                fused_srt,
                chinese_srt,
                translation_report,
                self.job_root / "translations",
                self.request.profile_id,
                provider,
                self.request.provider.model,
                self._translation_progress,
                concurrency=2,
            )
        chinese_cues = parse_srt(chinese_srt)
        self.check_cancelled()
        validate_timeline(fused_cues, chinese_cues)
        if api_config:
            _validate_api_translation(self.request, asr_report, fused_cues, chinese_cues, report, chinese_srt)
        _write_review_once(
            subtitle_review,
            _render_subtitle_review(fused_cues, chinese_cues),
        )
        for warning in report.get("warnings", []):
            self.events.emit("warning", code="TRANSLATION_WARNING", message=str(warning))
        self.events.emit("artifact_ready", kind="zh_srt", path=str(chinese_srt))
        self.events.emit(
            "artifact_ready", kind="subtitle_review_md", path=str(subtitle_review)
        )

        if package_checkpoint:
            self.events.emit("stage_completed", stage="packaging", resumed=True, overall_percent=97)
        else:
            self.packager_factory(tools.ffmpeg, tools.ffprobe).package(
                self.request.input_video, chinese_srt, final_mkv, technical_report, emit=self.events,
            )
            if api_config:
                validate_completed_mkv(final_mkv, technical_report, self.request.input_video, chinese_srt)
        self.events.emit("artifact_ready", kind="final_mkv", path=str(final_mkv))
        self.events.emit(
            "artifact_ready", kind="technical_report", path=str(technical_report)
        )
        completed = {
            "outputs": {
                "chinese_srt": str(chinese_srt),
                "subtitle_review_md": str(subtitle_review),
                "final_mkv": str(final_mkv),
                "technical_report": str(technical_report),
            },
            "technical_validation": {
                "status": "passed",
                "report_path": str(technical_report),
            },
            "human_review_status": "pending",
            "asr": asr_report,
        }
        if api_config:
            completed["provider"] = vars(api_config)
            completed["stages"] = {
                "asr": {"reused": asr_reused},
                "translation": {"reused": translation_checkpoint},
                "packaging": {"reused": package_checkpoint},
            }
        # Publish the primary result before an optional delivery can be stopped.
        _write_json(self.job_root / "job.result.json", completed)
        self.check_cancelled()
        if self.on_asr_ready is None and (api_config or self.request.additional_mp4):
            from .codex_runner import _append_mp4
            _append_mp4(self, completed)
            _write_json(self.job_root / "job.result.json", completed)
        self.events.emit("job_completed", **completed)
        return completed
