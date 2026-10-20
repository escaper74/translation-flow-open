"""Local desktop request/events adapter for the existing standalone SRT CLI."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from .contracts import ApiProviderRequest, JobRequest
from .events import EventEmitter
from .srt import parse_srt, subtitle_digest, validate_timeline
from .translation import cli as srt_cli
from .translation.codex_provider import TranslationCancelled


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_request(path: Path) -> JobRequest:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) - {
        "schema_version", "workflow_mode", "job_id", "input_srt", "output_dir",
        "profile_id", "provider", "consent",
    }:
        raise ValueError("SRT request 只能包含公开任务配置，密钥通过进程环境传入")
    if value.get("workflow_mode") != "srt_translation":
        raise ValueError("SRT 入口只支持 srt_translation")
    provider = value.get("provider", {})
    if not isinstance(provider, dict) or set(provider) - {
        "id", "host", "model", "endpoint_profile", "max_output_tokens",
    }:
        raise ValueError("provider 只能包含公开配置")
    if provider.get("id") == "codex" and set(provider) - {"id", "host", "model"}:
        raise ValueError("Codex 不使用 API 地域或输出上限配置")
    if value.get("consent", {}).get("provider") != provider.get("id"):
        raise ValueError("consent.provider 必须匹配本次翻译方式")
    mapped = dict(value, input_video=value.get("input_srt", ""))
    request = JobRequest.from_mapping(mapped)
    expected = request.output_dir / ".srt-translation" / request.job_id / "request.json"
    if path.resolve() != expected:
        raise ValueError("SRT request 必须位于输出目录的正式任务记录中")
    parse_srt(request.input_video)
    return request


def run(request: JobRequest, events: EventEmitter) -> dict:
    job = request.output_dir / ".srt-translation" / request.job_id
    source = parse_srt(request.input_video)
    identity = {
        "input": str(request.input_video), "source_sha256": subtitle_digest(source),
        "provider": asdict(request.provider), "profile": request.profile_id,
    }
    identity_path = job / "srt.input.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text(encoding="utf-8")) != identity:
            raise ValueError("SRT 源文或翻译配置与当前任务不一致，请新建任务")
    else:
        _write_json(identity_path, identity)
    output = request.output_dir / f"{request.input_video.stem[:80]}.{request.job_id}.zh-CN.srt"
    report_path = job / "translation.report.json"
    result_path = job / "job.result.json"
    signal = job / "cancel.requested"
    signal.unlink(missing_ok=True)
    events.emit("job_started", message="已接收 SRT 翻译任务")
    reused = output.exists() or report_path.exists()
    if reused:
        if not output.is_file() or not report_path.is_file():
            raise FileExistsError("SRT 与报告不完整；保留现场，拒绝覆盖已有结果")
    else:
        argv = [
            "--input", str(request.input_video), "--output", str(output),
            "--report", str(report_path), "--cache-dir", str(job / "cache"),
            "--provider", request.provider.id, "--model", request.provider.model,
            "--profile", request.profile_id,
            "--consent-subtitle-text-to-provider", request.provider.id,
        ]
        if isinstance(request.provider, ApiProviderRequest):
            argv += ["--endpoint-profile", request.provider.endpoint_profile,
                     "--max-output-tokens", str(request.provider.max_output_tokens)]
        events.emit("stage_started", stage="translation", overall_percent=0)
        phase_progress = {}
        def progress(item: dict) -> None:
            name = item.get("event", "")
            if name.startswith("polish"):
                stage, percent = "polish", 65
            elif name.startswith("kana"):
                stage, percent = "kana_repair", 90
            elif name.startswith("context") or name == "translation_started":
                stage, percent = "context", 5
            elif name == "translation_completed":
                stage, percent = "subtitle_validation", 98
            else:
                stage, percent = "translate_initial", 25
            current, total = item.get("current"), item.get("total")
            detail = f"{current}/{total}" if current is not None and total else ""
            if isinstance(current, int) and isinstance(total, int) and total > 0:
                span = {"context": 20, "translate_initial": 40, "polish": 25}.get(stage, 0)
                percent += span * current / total
            percent = max(percent, phase_progress.get(stage, 0))
            phase_progress[stage] = percent
            events.emit("stage_progress", stage=stage, overall_percent=percent,
                        detail=detail, resumed=item.get("resumed", False))
        status = srt_cli.main(argv, progress_callback=progress, cancelled=signal.exists,
                              log=lambda message: events.emit("diagnostic", message=message))
        if status == 2:
            raise TranslationCancelled("SRT 翻译已停止，成功批次已保留")
        if status:
            raise RuntimeError(f"SRT 翻译退出码为 {status}")
        events.emit("stage_completed", stage="translation", overall_percent=98)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    translated = parse_srt(output)
    validate_timeline(source, translated)
    expected = {"status": "passed", "input": str(request.input_video), "output": str(output),
                "source_sha256": subtitle_digest(source), "provider": request.provider.id,
                "model": request.provider.model, "profile": request.profile_id,
                "subtitle_count": len(source), "translated_count": len(source)}
    if request.provider.id != "codex":
        from .translation.api_providers import api_cache_identity
        expected["provider_identity"] = api_cache_identity(
            request.provider.id, getattr(request.provider, "endpoint_profile", None),
            getattr(request.provider, "max_output_tokens", 8192),
        )
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("SRT 翻译报告与源文或配置不一致")
    digest = subtitle_digest(translated)
    if report.get("output_sha256", digest) != digest:
        raise ValueError("已完成的 SRT 与报告摘要不一致")
    if result_path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("output_sha256") != digest:
            raise ValueError("已完成 SRT 被更改，拒绝复用")
    result = {"outputs": {"chinese_srt": str(output), "technical_report": str(report_path)},
              "technical_validation": {"status": "passed", "report_path": str(report_path)},
              "stages": {"translation": {"status": "completed", "reused": reused}},
              "output_sha256": digest, "reused": reused}
    _write_json(result_path, result)
    for warning in report.get("warnings", []):
        events.emit("warning", message=str(warning))
    events.emit("job_completed", **result)
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="桌面 SRT 翻译请求入口")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    events = None
    try:
        request = load_request(args.request)
        events = EventEmitter(request.job_id, args.request.parent / "events.jsonl")
        run(request, events)
        return 0
    except TranslationCancelled as exc:
        if events:
            events.emit("job_cancelled", message=str(exc))
        return 2
    except (Exception, SystemExit) as exc:
        # argparse diagnostics use stderr; they cannot be interpreted as JSONL success.
        if events:
            events.emit("job_failed", message=str(exc))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    finally:
        os.environ.pop("SUBTITLE_API_KEY", None)


if __name__ == "__main__":
    raise SystemExit(main())
