"""Original-picture Codex orchestration over the established local services."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from .asr import load_completed_asr
from .events import EventEmitter
from .packaging import validate_completed_mkv as _validate_mkv
from .runner import _render_subtitle_review, _review_cell, _safe_stem, _write_json, _write_review_once
from .srt import parse_srt, subtitle_digest, validate_timeline
from .translation.codex_provider import CodexProvider, TranslationCancelled
from .translation.source_review import prepare_codex_source


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"检查点必须是 JSON object：{path}")
    return value


def _stat(path: Path) -> dict[str, int]:
    value = path.stat()
    return {"size_bytes": value.st_size, "mtime_ns": value.st_mtime_ns}


def _job_identity(request: Any) -> dict[str, Any]:
    identity = {"input": str(request.input_video), **_stat(request.input_video),
                "profile": request.profile_id, "provider": vars(request.provider)}
    # Preserve existing large-v3 identities exactly; the candidate is explicit.
    if request.asr_a_model != "large-v3":
        identity["asr_a_model"] = request.asr_a_model
    return identity


def _check_cancelled(runner: Any) -> None:
    runner.check_cancelled()


def _append_mp4(runner: Any, completed: dict[str, Any]) -> None:
    if not runner.request.additional_mp4:
        completed.setdefault("stages", {}).setdefault("mp4_delivery", {"status": "not_requested"})
        return
    from media_planning import MediaRepackageRequest, MediaRepackageRunner, OutputTarget
    request = MediaRepackageRequest(1, runner.request.job_id,
                                    Path(completed["outputs"]["final_mkv"]),
                                    runner.request.output_dir, OutputTarget.MP4)
    _check_cancelled(runner)
    runner.events.emit("stage_started", stage="mp4_delivery", overall_percent=98,
                       detail="正在本地追加 MP4 可切换软字幕交付。")
    reused = request.output_path.is_file() and request.technical_report_path.is_file()
    try:
        if not reused:
            if request.output_path.exists() or request.technical_report_path.exists():
                raise FileExistsError("MP4 仅存在部分终态，保留 MKV 与现场并停止追加")
            MediaRepackageRunner(
                request, tool_root=runner.tool_root,
                emitter=EventEmitter(request.job_id, request.job_root / "events.jsonl", stdout=io.StringIO()),
            ).run()
        report = _load(request.technical_report_path)
        source = report.get("source", {})
        output = report.get("output", {})
        checks = report.get("technical_validation", {}).get("checks", {})
        if (report.get("status") != "technical_checks_passed"
                or Path(source.get("path", "")).resolve() != request.source_media.resolve()
                or any(source.get(key) != value for key, value in _stat(request.source_media).items())
                or Path(output.get("path", "")).resolve() != request.output_path
                or output.get("target") != "mp4" or not request.output_path.is_file()
                or not checks or any(value is not True for value in checks.values())):
            raise ValueError("MP4 与通过的技术报告不匹配")
        completed["outputs"].update(final_mp4=str(request.output_path),
                                    mp4_technical_report=str(request.technical_report_path))
        completed.setdefault("stages", {})["mp4_delivery"] = {"status": "technical_checks_passed", "reused": reused}
        runner.events.emit("artifact_ready", kind="final_mp4", path=str(request.output_path))
        runner.events.emit("stage_completed", stage="mp4_delivery", overall_percent=99, resumed=reused)
    except Exception as exc:
        completed.setdefault("stages", {})["mp4_delivery"] = {"status": "failed", "error": str(exc)}
        completed["outputs"].pop("final_mp4", None)
        completed["outputs"].pop("mp4_technical_report", None)
        runner.events.emit("warning", code="MP4_DELIVERY_FAILED", message=f"MKV 已完成，追加 MP4 失败：{exc}")


def _review_text(japanese: Any, chinese: Any, source_report: dict[str, Any], report: dict[str, Any]) -> str:
    text = _render_subtitle_review(japanese, chinese)
    text += "\n## Codex 与源文审查\n\n"
    text += "来源：Codex 官方 CLI，本机 ChatGPT 登录；模型策略 cli-default，实际模型未由 CLI 报告。\n\n"
    text += "本表中的日文为自动审查后的独立参考，不覆盖 A、C 或融合原件。翻译自然度与疑点仍需人工查看原片。\n\n"
    text += f"源文审查报告：{source_report.get('report_path', '')}\n\n"
    for warning in source_report.get("warnings", []):
        text += f"- {_review_cell(str(warning))}\n"
    uncertainties = [("源文", item) for item in source_report.get("items", []) if item.get("note", "").strip()]
    uncertainties += [(str(item.get("phase", "翻译")), item) for item in report.get("uncertainties", [])]
    text += "\n| ID | 阶段 | 待人工复核疑点 |\n|---|---|---|\n"
    for phase, item in uncertainties:
        text += f"| {item.get('id', '')} | {_review_cell(phase)} | {_review_cell(str(item.get('note', item.get('reason', ''))))} |\n"
    if not uncertainties:
        text += "| — | — | 自动审查未列出额外疑点；不等于真人验收通过。 |\n"
    return text


def validate_codex_translation(request: Any, job_root: Path, chinese: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate A/C -> reviewed Japanese -> Chinese without CLI or media tools."""
    identity = _job_identity(request)
    if _load(job_root / "codex-job.identity.json") != identity:
        raise ValueError("Codex 检查点的源媒体或翻译身份已改变")
    fused, asr_report = load_completed_asr(request.input_video, job_root / "asr",
                                         a_model_id=request.asr_a_model)
    pointer = _load(job_root / "source-review.pointer.json")
    reviewed = Path(pointer["reviewed_srt"])
    japanese, chinese_cues = parse_srt(reviewed), parse_srt(chinese)
    source_report = _load(Path(pointer["report_path"]))
    source_identity = source_report.get("identity", {})
    expected_paths = {"a": job_root / "asr/A/A.ja.srt", "c": job_root / "asr/C/C.ja.srt",
                      "fusion": fused, "asr_report": job_root / "asr/asr.report.json"}
    digests = {name: subtitle_digest(parse_srt(path)) for name, path in expected_paths.items() if name != "asr_report"}
    if (source_report.get("status") != "complete"
            or source_identity.get("provider_id") != "codex"
            or source_identity.get("model") != request.provider.model
            or source_identity.get("source_digests") != digests
            or source_report.get("timeline_preserved_from_a") is not True
            or Path(source_report.get("output_srt", "")).resolve() != reviewed.resolve()
            or Path(source_report.get("report_path", "")).resolve() != Path(pointer["report_path"]).resolve()
            or any(Path(source_report.get("input_paths", {}).get(name, "")).resolve() != path.resolve()
                   for name, path in expected_paths.items())
            or source_report.get("reviewed_source_sha256") != subtitle_digest(japanese)
            or pointer.get("reviewed_sha256") != subtitle_digest(japanese)):
        raise ValueError("Codex 源文审查与 A/C/fusion 或独立日文参考不匹配")
    accepted = {str(item["a_index"]): item["c_index"] for item in asr_report.get("accepted_matches", [])}
    if source_identity.get("accepted_matches") != accepted:
        raise ValueError("Codex 源文审查的融合记录与 ASR 报告不匹配")
    validate_timeline(parse_srt(fused), japanese)
    validate_timeline(japanese, chinese_cues)
    report = _load(job_root / "translation.report.json")
    if (report.get("status") != "passed" or report.get("provider") != "codex"
            or report.get("model") != request.provider.model or report.get("profile") != request.profile_id
            or Path(report.get("input", "")).resolve() != reviewed.resolve()
            or Path(report.get("output", "")).resolve() != chinese.resolve()
            or report.get("source_sha256") != subtitle_digest(japanese)
            or report.get("output_sha256") != subtitle_digest(chinese_cues)
            or report.get("subtitle_count") != len(japanese)
            or report.get("translated_count") != len(chinese_cues)):
        raise ValueError("Codex 翻译检查点与本次任务或中文字幕不匹配")
    return reviewed, report, source_report, asr_report


def run_codex_translation(runner: Any) -> dict[str, Any]:
    from .runner import ToolPaths
    request = runner.request
    longest_cache = runner.job_root / "translations" / "v1" / ("x" * 64) / "batches" / "kana_repair" / "000001_000060_0000000000.json"
    if os.name == "nt" and len(str(longest_cache)) >= 260:
        raise ValueError("输出目录过深，Windows 无法写入字幕批次缓存；请使用较短的输出目录（例如磁盘根目录下的成片文件夹）。尚未调用 Codex 或运行 ASR。")
    runner.api_key = ""
    request.output_dir.mkdir(parents=True, exist_ok=True)
    runner.job_root.mkdir(parents=True, exist_ok=True)
    # This marker belongs to the previous invocation of this exact task. UI only
    # enables stop after the new backend has emitted job_started.
    (runner.job_root / "cancel.requested").unlink(missing_ok=True)
    _check_cancelled(runner)
    identity = _job_identity(request)
    identity_path = runner.job_root / "codex-job.identity.json"
    if identity_path.exists():
        if _load(identity_path) != identity:
            raise ValueError("任务源媒体或翻译身份已改变，拒绝复用旧 Codex 检查点")
    else:
        _write_json(identity_path, identity)
    stem = f"{_safe_stem(request.input_video.stem)}.{request.profile_id.replace('_', '-')}.{_safe_stem(request.job_id)[:12]}"
    chinese = request.output_dir / f"{stem}.zh-CN.srt"
    review = request.output_dir / f"{stem}.subtitle-review.md"
    mkv = request.output_dir / f"{stem}.zh-CN.mkv"
    translation_path = runner.job_root / "translation.report.json"
    technical_path = runner.job_root / "technical-validation.report.json"
    pointer_path = runner.job_root / "source-review.pointer.json"
    result_path = runner.job_root / "job.result.json"
    if chinese.exists() != translation_path.exists():
        raise FileExistsError("中文字幕与翻译报告不完整，保留现场并停止")
    if mkv.exists() != technical_path.exists():
        raise FileExistsError("MKV 与技术报告不完整，保留现场并停止")
    runner.events.emit("job_started", profile_id=request.profile_id, provider="codex", input_video=str(request.input_video))

    if chinese.is_file():
        reviewed, report, source_report, asr_report = validate_codex_translation(request, runner.job_root, chinese)
        japanese = parse_srt(reviewed)
        if mkv.is_file():
            _validate_mkv(mkv, technical_path, request.input_video, chinese)
        _check_cancelled(runner)
        if runner.on_asr_ready is not None:
            runner.on_asr_ready()
        runner.events.emit("stage_completed", stage="subtitle_validation", resumed=True, overall_percent=82)
    else:
        provider_factory = runner.codex_provider_factory or CodexProvider
        provider = provider_factory(audit_dir=runner.job_root / "codex-calls", model=request.provider.model,
                                    cancelled=runner.cancel_requested)
        runner.events.emit("stage_started", stage="preflight", overall_percent=0)
        provider.check_connection()
        runner.events.emit("stage_completed", stage="preflight", overall_percent=1)
        class AsrEvents:
            def emit(self, event: str, **payload: Any) -> None:
                # Keep stage boundaries covered in addition to process polling.
                if event == "stage_started":
                    _check_cancelled(runner)
                runner.events.emit(event, **payload)
        asr_root = runner.job_root / "asr"
        if (asr_root / "asr.report.json").is_file():
            fused, asr_report = load_completed_asr(request.input_video, asr_root, AsrEvents(),
                                                 a_model_id=request.asr_a_model)
        else:
            tools = ToolPaths.from_root(runner.tool_root, runner.tool_root / "asr-lab", python=runner.python,
                                        a_model_id=request.asr_a_model)
            missing = [str(path) for path in tools.required_paths() if not path.exists()]
            if missing:
                raise FileNotFoundError("正式流程所需本地工具缺失：" + ", ".join(missing))
            runner.asr_factory(tools).run(request.input_video, asr_root, emit=AsrEvents(),
                                         cancelled=runner.cancel_requested)
            fused, asr_report = load_completed_asr(request.input_video, asr_root,
                                                 a_model_id=request.asr_a_model)
        _check_cancelled(runner)
        if runner.on_asr_ready is not None:
            runner.on_asr_ready()
        runner.events.emit("stage_started", stage="source_review", overall_percent=47)
        prepare = runner.source_preparer or prepare_codex_source
        reviewed, source_report = prepare(fused, asr_report, runner.job_root / "source-review", provider,
                                          request.provider.model, runner._translation_progress)
        japanese = parse_srt(reviewed)
        validate_timeline(parse_srt(fused), japanese)
        _write_json(pointer_path, {"reviewed_srt": str(reviewed), "reviewed_sha256": subtitle_digest(japanese),
                                   "report_path": source_report["report_path"]})
        runner.events.emit("stage_completed", stage="source_review", overall_percent=48)
        report = runner.translator(reviewed, chinese, translation_path, runner.job_root / "translations",
                                    request.profile_id, provider, request.provider.model,
                                    runner._translation_progress, concurrency=2)
    _check_cancelled(runner)
    chinese_cues = parse_srt(chinese)
    validate_timeline(japanese, chinese_cues)
    _write_review_once(review, _review_text(japanese, chinese_cues, source_report, report))
    for warning in [*source_report.get("warnings", []), *report.get("warnings", [])]:
        runner.events.emit("warning", code="TRANSLATION_WARNING", message=str(warning))
    for kind, path in (("zh_srt", chinese), ("subtitle_review_md", review), ("source_review", Path(source_report["report_path"]))):
        runner.events.emit("artifact_ready", kind=kind, path=str(path))
    reused = mkv.is_file()
    if not reused:
        tools = ToolPaths.from_root(runner.tool_root, runner.tool_root / "asr-lab", python=runner.python)
        runner.packager_factory(tools.ffmpeg, tools.ffprobe, subtitle_title="Simplified Chinese · Codex").package(
            request.input_video, chinese, mkv, technical_path, emit=runner.events)
    _validate_mkv(mkv, technical_path, request.input_video, chinese)
    runner.events.emit("artifact_ready", kind="final_mkv", path=str(mkv))
    completed = {"provider": "codex", "outputs": {"chinese_srt": str(chinese), "subtitle_review_md": str(review),
                 "reviewed_japanese_srt": str(reviewed), "source_review_report": source_report["report_path"],
                 "final_mkv": str(mkv), "technical_report": str(technical_path)},
                 "technical_validation": {"status": "passed", "report_path": str(technical_path)},
                 "human_review_status": "pending", "asr": asr_report,
                 "stages": {"mkv_packaging": {"status": "technical_checks_passed", "reused": reused}}}
    # Save the primary delivery before optional repackage/cancellation. A later
    # invocation can add MP4 without contacting Codex or running ASR again.
    _write_json(result_path, completed)
    if runner.on_asr_ready is None:
        _append_mp4(runner, completed)
    _write_json(result_path, completed)
    runner.events.emit("job_completed", **completed)
    return completed
