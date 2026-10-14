"""Local delivery library and explicit post-delivery actions; never an ASR/API path."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from .srt import parse_srt, subtitle_digest, write_srt


RESULTS = {".full-workflow": "workflow.result.json", ".translation-flow": "job.result.json",
           ".picture-workflow": "job.result.json", ".srt-translation": "job.result.json",
           ".media-repackage": "technical-validation.report.json"}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"记录不是 JSON 对象：{path}")
    return value


def no_links(path: Path) -> Path:
    """Check the lexical path before resolve, including junction-bearing parents."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"不操作符号链接或目录联接：{part}")
    return path.resolve()


def signature(path: Path) -> dict:
    info = no_links(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"不是普通文件：{path}")
    return {"path": str(path.resolve()), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def load_delivery(request_path: str | Path) -> dict:
    request_path = no_links(Path(request_path))
    job = request_path.parent
    folder = job.parent.name
    if request_path.name != "request.json" or folder not in RESULTS:
        raise ValueError("请选择正式任务目录中的 request.json")
    request = read_json(request_path)
    root = no_links(Path(request["output_dir"]))
    if job.parent.parent != root or job.name != request["job_id"]:
        raise ValueError("任务记录与输出目录不一致")
    result_path = job / RESULTS[folder]
    result = read_json(result_path)
    if not (result.get("status") == "technical_checks_passed" or
            result.get("technical_validation", {}).get("status") == "passed"):
        raise ValueError("任务尚未技术完成，保留全部检查点")
    outputs = result.get("outputs", {})
    if folder == ".media-repackage":
        outputs = {"final_media": result["output"]["path"]}
    variants = []
    for key in ("final_mp4", "final_mkv", "final_media", "translation_original_mkv"):
        raw = outputs.get(key)
        if raw and Path(raw).is_file():
            path = no_links(Path(raw))
            if not path.is_relative_to(root):
                raise ValueError("成品不在本任务输出目录内")
            if str(path) not in variants:
                variants.append(str(path))
    source = request.get("input_video", request.get("input_srt", request.get("source_media", "")))
    chinese = outputs.get("chinese_srt", "")
    if not variants and not (chinese and Path(chinese).is_file()):
        raise ValueError("成品已移动或不存在")
    return dict(request_path=str(request_path), job_root=str(job), output_dir=str(root),
                job_id=job.name, source=source, title=Path(source).name, kind=folder,
                media=variants[0] if variants else "", variants=variants,
                chinese_srt=chinese, outputs=outputs, result=result,
                result_path=str(result_path), updated=result_path.stat().st_mtime,
                cleaned=(job / "retention-cleanup.json").exists(),
                status="技术完成" if variants else "字幕完成")


def list_deliveries(root: str | Path) -> dict:
    root = no_links(Path(root))
    if not root.is_dir():
        return {"items": [], "warnings": ["保存目录不存在"]}
    # Old output layouts sometimes have a single title directory below the root.
    scopes = [root] + [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                       and not p.is_symlink() and not getattr(p, "is_junction", lambda: False)()]
    items, warnings = [], []
    for scope in scopes:
        for folder in RESULTS:
            base = scope / folder
            if not base.is_dir():
                continue
            try:
                no_links(base)
                for job in base.iterdir():
                    request = job / "request.json"
                    if not request.is_file() or not (job / RESULTS[folder]).is_file():
                        continue
                    try:
                        item = load_delivery(request)
                        items.append(item)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        warnings.append(f"{job.name}: {exc}")
            except (OSError, ValueError) as exc:
                warnings.append(str(exc))
    full = {(x["output_dir"], x["job_id"]) for x in items if x["kind"] == ".full-workflow"}
    items = [x for x in items if x["kind"] != ".translation-flow" or (x["output_dir"], x["job_id"]) not in full]
    for item in items:
        item.pop("result")
        item.pop("outputs")
        item["size_gib"] = round(Path(item["media"]).stat().st_size / 2**30, 2) if item["media"] else 0
    return {"items": sorted(items, key=lambda x: x["updated"], reverse=True), "warnings": warnings}


def cleanup_plan(request_path: str | Path) -> dict:
    item = load_delivery(request_path)
    job, root = Path(item["job_root"]), Path(item["output_dir"])
    if not item["media"]:
        raise ValueError("独立字幕任务没有大媒体检查点需要清理")
    # The normal queue holds this same lease throughout the backend run.
    events = job / "events.jsonl"
    if not events.is_file():
        raise ValueError("缺少终态事件，暂不提供自动清理")
    last = None
    with events.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                last = json.loads(line)
    if not last or last.get("event") != "job_completed":
        raise ValueError("任务仍在运行或未完整结束，保留检查点")
    protected = {Path(p).resolve() for p in item["variants"]}
    if item["source"]:
        protected.add(Path(item["source"]).resolve())
    candidates = set()
    picture = job / "picture"
    if item["kind"] in (".full-workflow", ".picture-workflow"):
        report_path = picture / "reports/workflow-report.json"
        if report_path.is_file():
            report = read_json(report_path)
            if report.get("status") != "technical_checks_passed":
                raise ValueError("画面阶段报告尚未通过")
            raw = report.get("output")
            if raw:
                if not str(raw).endswith(".video-only.mkv"):
                    raise ValueError("画面检查点不符合正式纯视频布局，请保留并单独检查")
                candidates.add(Path(raw))
        raw = item["outputs"].get("processed_video")
        if raw:
            if not str(raw).endswith(".video-only.mkv"):
                raise ValueError("画面检查点不符合正式纯视频布局，请保留并单独检查")
            candidates.add(Path(raw))
        for name in ("segments_input", "segments_restored"):
            directory = picture / name
            if directory.is_dir():
                no_links(directory)
                for path in directory.iterdir():
                    if re.fullmatch(r"segment_\d{4,}(?:\.[\w-]+)*\.mkv", path.name):
                        candidates.add(path)
    # Only job-local WAV files; never chase arbitrary external ASR/model paths.
    translation = root / ".translation-flow" / item["job_id"] if item["kind"] == ".full-workflow" else job
    asr = translation / "asr"
    if asr.is_dir():
        for directory, dirs, files in os.walk(no_links(asr), followlinks=False):
            for name in dirs:
                no_links(Path(directory) / name)
            for name in files:
                if name.lower().endswith(".wav"):
                    candidates.add(Path(directory) / name)
    files = []
    for path in sorted(candidates):
        path = no_links(path)
        if not path.is_relative_to(root) or path in protected:
            raise ValueError(f"候选越出任务目录或命中保留文件：{path}")
        if path.exists():
            files.append(signature(path))
    return {"request_path": item["request_path"], "files": files, "count": len(files),
            "bytes": sum(x["bytes"] for x in files),
            "retained": [signature(Path(p)) for p in item["variants"]],
            "warning": "永久删除列出的媒体检查点，不能恢复这些分段；保留全部成片版本、字幕、翻译缓存、日志和源片。外部 ASR 临时目录不在本次清理范围。"}


def clean_checkpoints(plan: dict, *, confirmed: bool = False) -> dict:
    if confirmed is not True:
        raise ValueError("请先查看精确清单并确认永久删除")
    from .task_queue import FileLease, write_json
    request = no_links(Path(plan["request_path"]))
    with FileLease(request.parent / "queue.run.lock"):
        current = cleanup_plan(request)
        if current != plan:
            raise ValueError("文件或任务状态已变化，请重新查看清单后确认")
        if not current["files"]:
            return {"status": "nothing_to_clean", "deleted_count": 0}
        # Entire list validated before the first unlink; never recurse or broaden on failure.
        record = {"status": "cleaning", "plan": current, "deleted": [], "error": ""}
        record_path = request.parent / "retention-cleanup.json"
        before = shutil.disk_usage(request.parent).free
        write_json(record_path, record)
        try:
            for entry in current["files"]:
                path = Path(entry["path"])
                if signature(path) != entry:
                    raise ValueError(f"文件变化，已停止：{path}")
                path.unlink()
                record["deleted"].append(entry["path"])
            record["status"] = "completed"
        except Exception as exc:
            record["status"] = "partial"
            record["error"] = str(exc)
            raise
        finally:
            record["deleted_count"] = len(record["deleted"])
            record["free_increase_bytes"] = shutil.disk_usage(request.parent).free - before
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            write_json(record_path, record)
    return record


def load_review(request_path: str | Path) -> dict:
    item = load_delivery(request_path)
    if not item["chinese_srt"]:
        raise ValueError("这个成品没有可编辑的独立 SRT；已有画面硬字幕不属于字幕校对范围")
    chinese_path = no_links(Path(item["chinese_srt"]))
    chinese = parse_srt(chinese_path)
    root, job = Path(item["output_dir"]), Path(item["job_root"])
    translation = root / ".translation-flow" / item["job_id"] if item["kind"] == ".full-workflow" else job
    candidates = []
    result_path = translation / "job.result.json"
    if result_path.is_file():
        outputs = read_json(result_path).get("outputs", {})
        if outputs.get("reviewed_japanese_srt"):
            candidates.append(Path(outputs["reviewed_japanese_srt"]))
    candidates += [translation / "asr/fusion/fusion.ja.srt"]
    if item["kind"] == ".srt-translation":
        candidates.append(Path(item["source"]))
    japanese = {}
    for path in candidates:
        if path.is_file():
            japanese = {c.index: c for c in parse_srt(path)}
            break
    origin, duration = 0, None
    if item["kind"] == ".full-workflow":
        report = read_json(Path(item["outputs"]["technical_report"]))
        origin, duration = int(report["source_start_ms"]), int(report["processed_duration_ms"])
    rows = []
    for cue in chinese:
        start, end = max(0, cue.start_ms-origin), cue.end_ms-origin
        if duration is not None:
            end = min(duration, end)
        if end <= start:
            continue
        ja = japanese.get(cue.index)
        matched = ja and (ja.start_ms, ja.end_ms) == (cue.start_ms, cue.end_ms)
        rows.append(dict(id=cue.index, start_ms=start, end_ms=end,
                         time=f"{start//3600000:02}:{start//60000%60:02}:{start//1000%60:02}.{start%1000:03}",
                         japanese=ja.text if matched else "（无匹配日文参考）", chinese=cue.text))
    return dict(request_path=item["request_path"], chinese_srt=str(chinese_path),
                source_digest=subtitle_digest(chinese), media=item["media"], rows=rows,
                media_signature=signature(Path(item["media"])) if item["media"] else None)


def save_review(review: dict) -> dict:
    current = load_review(review["request_path"])
    if current["source_digest"] != review.get("source_digest") or current["media_signature"] != review.get("media_signature"):
        raise ValueError("原字幕或成片发生变化，请重新打开校对页")
    rows = review["rows"]
    if len(rows) != len(current["rows"]):
        raise ValueError("本轮校对只修改文字，不增删字幕或改时间轴")
    from .srt import SubtitleCue
    cues, changes = [], []
    for row, old in zip(rows, current["rows"], strict=True):
        if any(row.get(key) != old[key] for key in ("id", "start_ms", "end_ms")):
            raise ValueError("字幕编号或时间轴发生变化")
        text = str(row["chinese"]).strip()
        if not text:
            raise ValueError(f"第 {old['id']} 条字幕不能为空")
        cues.append(SubtitleCue(old["id"], old["start_ms"], old["end_ms"], text))
        if text != old["chinese"]:
            changes.append(old["id"])
    # Validate in memory first; write_srt supplies the same SRT formatting validation.
    if any(re.search(r"\n[ \t]*\n", c.text.replace("\r\n", "\n")) for c in cues):
        raise ValueError("字幕正文不能含空白行")
    item = load_delivery(review["request_path"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = no_links(Path(item["output_dir"]) / ".subtitle-edits" / stamp)
    directory.mkdir(parents=True, exist_ok=False)
    srt = write_srt(directory / "revised.zh-CN.srt", cues)
    record = {"kind": "human_subtitle_revision", "request_path": item["request_path"],
              "original_srt": current["chinese_srt"], "source_digest": current["source_digest"],
              "chinese_srt": str(srt), "subtitle_digest": subtitle_digest(parse_srt(srt)),
              "media": current["media"], "media_signature": current["media_signature"],
              "changed_ids": changes, "human_review_status": "edited_not_accepted"}
    path = directory / "revision.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    return {"revision_path": str(path), "chinese_srt": str(srt), "changed_count": len(changes)}


def export_review(revision_path: str, tool_root: str) -> dict:
    from .asr import ToolPaths
    from .packaging import MkvSoftSubtitlePackager
    path = no_links(Path(revision_path))
    record = read_json(path)
    if record.get("kind") != "human_subtitle_revision":
        raise ValueError("请选择人工修订记录")
    source = no_links(Path(record["media"]))
    chinese = no_links(Path(record["chinese_srt"]))
    if signature(source) != record["media_signature"] or subtitle_digest(parse_srt(chinese)) != record["subtitle_digest"]:
        raise ValueError("修订字幕或成片已改变，请重新保存修订")
    item = load_delivery(record["request_path"])
    if path.parent.parent != Path(item["output_dir"]) / ".subtitle-edits" or chinese.parent != path.parent:
        raise ValueError("修订记录路径不在本任务修订目录内")
    output = Path(item["output_dir"]) / f"{source.stem}.revised-{path.parent.name}.mkv"
    tools = ToolPaths.from_root(Path(tool_root), Path(tool_root) / "asr-lab", python=Path(sys.executable))
    packager = MkvSoftSubtitlePackager(tools.ffmpeg, tools.ffprobe, subtitle_title="简体中文 · 人工修订")
    probe = packager._probe(source)
    subs = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    if len(subs) > 1 or (subs and subs[0].get("tags", {}).get("language") not in ("zho", "chi", "zh")):
        raise ValueError("成片含额外或非中文字幕轨；本轮不自动替换，请只导出修订 SRT")
    report = path.parent / "packaging.report.json"
    packager.package(source, chinese, output, report)
    if signature(source) != record["media_signature"]:
        raise ValueError("封装期间输入成片改变，请保留现场检查")
    return {"media": str(output), "technical_report": str(report), "human_review_status": "pending"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raw = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        action = raw["action"]
        if action == "list":
            value = list_deliveries(raw["output_dir"])
        elif action == "plan":
            value = cleanup_plan(raw["request_path"])
        elif action == "clean":
            value = clean_checkpoints(raw["plan"], confirmed=raw.get("confirmed") is True)
        elif action == "review":
            value = load_review(raw["request_path"])
        elif action == "save_review":
            value = save_review(raw["review"])
        elif action == "export_review":
            value = export_review(raw["revision_path"], raw["tool_root"])
        else:
            raise ValueError("不支持的成品操作")
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
