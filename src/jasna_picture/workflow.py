from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Iterable
from translation_flow.cancellation import check_cancelled

from .plan import build_jasna_command
from .runner import (
    ExperimentError,
    _assert_close_duration,
    _assert_extracted_total,
    _decode_spots,
    _duration,
    _ensure_ascii_install_path,
    _gpu_utilization_percent,
    _probe,
    _require_file,
    _run_streamed,
    _stream_signature,
    _video_stream,
    _write_json,
)


WORKFLOW_SCHEMA_VERSION = 1
EXPECTED_JASNA_VERSION = "0.10.0"
ACCEPTANCE_DURATION_SECONDS = 30 * 60
DEFAULT_SEGMENT_SECONDS = 15 * 60
SEGMENT_TIMEOUT_REALTIME_FACTOR = 1.25
SEGMENT_STALL_SECONDS = 60.0
IDLE_GPU_THRESHOLD_PERCENT = 10
EXPECTED_OUTPUT_CODEC = "hevc"
FULL_DURATION_TOLERANCE_SECONDS = 2.0

BASICVSR_ENGINE_NAMES = (
    "loop_body_backward_1.trt_fp16.win.engine",
    "loop_body_forward_1.trt_fp16.win.engine",
    "loop_body_backward_2.trt_fp16.win.engine",
    "loop_body_forward_2.trt_fp16.win.engine",
    "preprocess_b60.trt_fp16.win.engine",
    "upsample_dyn_b30.trt_fp16.win.engine",
)
YOLO_ENGINE_NAME = "lada_mosaic_detection_model_v4_fast.fp16.win.engine"


@dataclass(frozen=True)
class PictureWorkflowConfig:
    scope: str
    source: Path
    work_dir: Path
    output: Path
    jasna: Path
    ffmpeg: Path
    ffprobe: Path
    gate_report: Path
    start: str | None = None
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS

    @property
    def inputs_dir(self) -> Path:
        return self.work_dir / "segments_input"

    @property
    def restored_dir(self) -> Path:
        return self.work_dir / "segments_restored"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.work_dir / "reports"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "workflow.json"

    @property
    def prepare_report_path(self) -> Path:
        return self.reports_dir / "prepare.json"

    @property
    def final_report_path(self) -> Path:
        return self.reports_dir / "workflow-report.json"

    @property
    def concat_manifest_path(self) -> Path:
        return self.work_dir / "concat.txt"

    @property
    def input_pattern(self) -> Path:
        return self.inputs_dir / "segment_%04d.mkv"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "source": str(self.source),
            "work_dir": str(self.work_dir),
            "output": str(self.output),
            "jasna": str(self.jasna),
            "ffmpeg": str(self.ffmpeg),
            "ffprobe": str(self.ffprobe),
            "gate_report": str(self.gate_report),
            "start": self.start,
            "duration_seconds": (
                ACCEPTANCE_DURATION_SECONDS if self.scope == "acceptance" else None
            ),
            "segment_seconds": self.segment_seconds,
        }


def _validate_config(config: PictureWorkflowConfig) -> None:
    if config.scope not in {"acceptance", "full"}:
        raise ExperimentError(f"未知 workflow scope：{config.scope}")
    if config.segment_seconds <= 0:
        raise ExperimentError("segment_seconds 必须为正数。")
    if config.output.suffix.casefold() != ".mkv":
        raise ExperimentError("正式画面 workflow 输出必须使用 .mkv 容器。")
    if config.source.resolve() == config.output.resolve():
        raise ExperimentError("源片与输出不能是同一个文件。")
    if config.scope == "acceptance":
        if not config.start:
            raise ExperimentError("30 分钟验收必须显式指定 --start。")
        if config.segment_seconds != DEFAULT_SEGMENT_SECONDS:
            raise ExperimentError("30 分钟验收固定使用两个 15 分钟段。")
    elif config.start is not None:
        raise ExperimentError("整片 workflow 不接受 --start；必须从源片开头处理完整视频轨。")


def required_engine_paths(jasna: Path) -> tuple[Path, ...]:
    weights = jasna.parent / "model_weights"
    basic_dir = weights / "lada_mosaic_restoration_model_generic_v1.2_sub_engines"
    basic = tuple(basic_dir / name for name in BASICVSR_ENGINE_NAMES)
    return basic + (weights / YOLO_ENGINE_NAME,)


def _first_output_line(command: list[str], label: str) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        raise ExperimentError(f"{label} 预检失败，退出码 {result.returncode}：\n{output}")
    lines = output.splitlines()
    return lines[0] if lines else ""


def _source_video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        raise ExperimentError(
            f"源片必须恰好有一条主要视频轨，实际为 {len(videos)} 条：{probe.get('path')}"
        )
    return videos[0]


def _frozen_route() -> dict[str, Any]:
    return {
        "detector": "lada-yolo-v4",
        "restorer": "basicvsrpp",
        "codec": EXPECTED_OUTPUT_CODEC,
        "cq": 28,
        "max_clip_size": 90,
        "temporal_overlap": 8,
    }


def _gate_lineage(config: PictureWorkflowConfig, report: dict[str, Any]) -> dict[str, Any]:
    """Validate the accepted global route gate and record its source evidence."""

    source_identity = report.get("source_identity")
    route = report.get("route")
    lineage_source = "report"
    if not isinstance(source_identity, dict) or not isinstance(route, dict):
        # The already accepted 2026-08-17 report predates embedded lineage. Its
        # sibling formal manifest is the authoritative source/route record.
        legacy_manifest = config.gate_report.parent.parent / "workflow.json"
        legacy = _load_json(legacy_manifest, "前级验收 workflow manifest")
        if (
            legacy.get("scope") != "acceptance"
            or Path(str(legacy.get("output", ""))).resolve()
            != Path(str(report.get("output", ""))).resolve()
        ):
            raise ExperimentError("前级验收报告与其 workflow manifest 不匹配。")
        source_identity = legacy.get("source")
        route = legacy.get("route")
        lineage_source = str(legacy_manifest)

    if not isinstance(source_identity, dict) or not isinstance(route, dict):
        raise ExperimentError("前级验收证据缺少源片身份或冻结画面路线。")
    expected_source = _source_identity(config)
    try:
        same_path = Path(str(source_identity.get("path", ""))).resolve() == Path(
            expected_source["path"]
        ).resolve()
    except (OSError, ValueError):
        same_path = False
    if route != _frozen_route():
        raise ExperimentError("前级 30 分钟验收路线与当前冻结 Jasna 路线不匹配。")

    source_match = {
        "path": same_path,
        "size": source_identity.get("size") == expected_source["size"],
        "mtime_ns": source_identity.get("mtime_ns") == expected_source["mtime_ns"],
    }
    same_source = all(source_match.values())
    informational_warnings = []
    if not same_source:
        informational_warnings.append(
            {
                "code": "GLOBAL_ROUTE_GATE_SOURCE_DIFFERS",
                "message": (
                    "全局路线 Gate 来自另一源片；它只证明冻结 Jasna 路线"
                    "已获验收，不表示当前源片完成了逐片 30 分钟人工验收。"
                ),
            }
        )
    return {
        "policy": "global_frozen_route_gate",
        "gate_report": str(config.gate_report.resolve()),
        "gate_status": report.get("status"),
        "gate_scope": report.get("scope"),
        "source_identity": source_identity,
        "route": route,
        "lineage_source": lineage_source,
        "source_identity_match": {**source_match, "all": same_source},
        "current_source_30m_human_reviewed": same_source,
        "informational_warnings": informational_warnings,
    }


def _persist_global_route_gate_evidence(
    config: PictureWorkflowConfig,
    report: dict[str, Any],
    gate_lineage: dict[str, Any] | None,
) -> dict[str, Any]:
    if config.scope != "full":
        return report
    if not isinstance(gate_lineage, dict):
        raise ExperimentError("整片报告缺少全局冻结路线 Gate 证据。")

    enriched = {
        **report,
        "release_status": "candidate_for_human_review",
        "human_review_status": "pending",
        "route_gate": gate_lineage,
        "current_source_30m_human_reviewed": bool(
            gate_lineage.get("current_source_30m_human_reviewed", False)
        ),
        "informational_warnings": [
            *list(report.get("informational_warnings", [])),
            *list(gate_lineage.get("informational_warnings", [])),
        ],
    }
    _write_json(config.final_report_path, enriched)
    return enriched


def preflight_workflow(config: PictureWorkflowConfig) -> dict[str, Any]:
    _validate_config(config)
    _require_file(config.source, "源片")
    _require_file(config.jasna, "Jasna")
    _require_file(config.ffmpeg, "ffmpeg")
    _require_file(config.ffprobe, "ffprobe")
    _require_file(config.gate_report, "前级验收报告")
    _ensure_ascii_install_path(config.jasna)

    engine_records: list[dict[str, Any]] = []
    for engine in required_engine_paths(config.jasna):
        _require_file(engine, "TensorRT 引擎")
        size = engine.stat().st_size
        if size <= 0:
            raise ExperimentError(f"TensorRT 引擎为空：{engine}")
        engine_records.append({"path": str(engine), "size": size})

    jasna_version = _first_output_line([str(config.jasna), "--version"], "Jasna")
    if EXPECTED_JASNA_VERSION not in jasna_version:
        raise ExperimentError(
            f"workflow 固定为 Jasna {EXPECTED_JASNA_VERSION}，当前返回：{jasna_version!r}"
        )
    ffmpeg_version = _first_output_line([str(config.ffmpeg), "-version"], "ffmpeg")
    ffprobe_version = _first_output_line([str(config.ffprobe), "-version"], "ffprobe")
    source_probe = _probe(config.ffprobe, config.source)
    source_stream = _source_video_stream(source_probe)
    gate_report = _load_json(config.gate_report, "前级验收报告")
    if gate_report.get("status") != "technical_checks_passed":
        raise ExperimentError(f"前级技术检查没有通过：{config.gate_report}")
    if config.scope == "full" and gate_report.get("scope") != "acceptance":
        raise ExperimentError("整片预检要求 scope=acceptance 的 30 分钟技术报告。")
    gate_lineage = (
        _gate_lineage(config, gate_report) if config.scope == "full" else None
    )

    return {
        "status": "preflight_passed",
        "jasna_version": jasna_version,
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "engines": engine_records,
        "source_duration_seconds": _duration(source_probe),
        "source_video_stream": source_stream,
        "gate_lineage": gate_lineage,
    }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ExperimentError(f"{label}必须是 JSON object：{path}")
    return payload


def _require_gate(
    config: PictureWorkflowConfig,
    *,
    confirm_gpu_ready: bool,
    confirm_gate_viewed: bool,
) -> dict[str, Any]:
    if not confirm_gpu_ready:
        raise ExperimentError(
            "该 workflow 会使用 GPU。确认 Whisper A/C 已退出且不会打扰现有任务后，"
            "显式传入 --confirm-gpu-ready。"
        )
    report = _load_json(config.gate_report, "前级验收报告")
    if report.get("status") != "technical_checks_passed":
        raise ExperimentError(f"前级技术检查没有通过：{config.gate_report}")
    if not confirm_gate_viewed:
        raise ExperimentError(
            "必须确认用户已经观看并认可前级样片，才可显式传入 --confirm-gate-viewed。"
        )
    if config.scope == "full" and report.get("scope") != "acceptance":
        raise ExperimentError(
            "整片 workflow 只能由 30 分钟 acceptance 报告放行，不能直接使用 60 秒报告。"
        )
    if config.scope == "full":
        _gate_lineage(config, report)
    return report


def _source_identity(config: PictureWorkflowConfig) -> dict[str, Any]:
    stat = config.source.stat()
    return {
        "path": str(config.source.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _manifest_payload(config: PictureWorkflowConfig) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "source": _source_identity(config),
        "scope": config.scope,
        "start": config.start,
        "duration_seconds": (
            ACCEPTANCE_DURATION_SECONDS if config.scope == "acceptance" else None
        ),
        "segment_seconds": config.segment_seconds,
        "output": str(config.output.resolve()),
        "jasna": str(config.jasna.resolve()),
        "route": _frozen_route(),
    }


def _load_or_create_manifest(config: PictureWorkflowConfig) -> dict[str, Any]:
    expected = _manifest_payload(config)
    if config.manifest_path.exists():
        actual = _load_json(config.manifest_path, "workflow manifest")
        if actual != expected:
            raise ExperimentError(
                "work_dir 已属于另一份源片、范围、输出或参数，拒绝混用："
                f"{config.manifest_path}"
            )
        return actual

    if config.work_dir.exists() and any(config.work_dir.iterdir()):
        raise ExperimentError(
            "work_dir 非空但没有 workflow manifest，拒绝猜测其中产物归属："
            f"{config.work_dir}"
        )
    config.work_dir.mkdir(parents=True, exist_ok=True)
    _write_json(config.manifest_path, expected)
    return expected


def _next_log_path(logs_dir: Path, stem: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    first = logs_dir / f"{stem}.log"
    if not first.exists():
        return first
    attempt = 2
    while True:
        candidate = logs_dir / f"{stem}.attempt{attempt:02d}.log"
        if not candidate.exists():
            return candidate
        attempt += 1


def _successful_streamed_log(
    logs_dir: Path, stem: str
) -> tuple[Path, float] | None:
    candidates = sorted(
        logs_dir.glob(f"{stem}*.log"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        return_code: int | None = None
        elapsed_seconds: float | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("return_code="):
                try:
                    return_code = int(line.split("=", 1)[1].split()[0])
                except (IndexError, ValueError):
                    return_code = None
            elif line.startswith("elapsed_seconds="):
                try:
                    elapsed_seconds = float(line.split("=", 1)[1].split()[0])
                except (IndexError, ValueError):
                    elapsed_seconds = None
        if (
            return_code == 0
            and elapsed_seconds is not None
            and math.isfinite(elapsed_seconds)
            and elapsed_seconds >= 0
        ):
            return path, elapsed_seconds
    return None


def _preserve_interrupted_file(path: Path) -> Path:
    """Move an unproven working file aside without deleting evidence."""

    attempt = 1
    while True:
        candidate = path.with_name(
            f"{path.stem}.interrupted-{attempt:02d}{path.suffix}"
        )
        if not candidate.exists():
            path.replace(candidate)
            return candidate
        attempt += 1


def build_prepare_command(config: PictureWorkflowConfig) -> tuple[str, ...]:
    command = [
        str(config.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-n",
    ]
    if config.scope == "acceptance":
        command.extend(["-ss", str(config.start)])
    command.extend(["-i", str(config.source)])
    if config.scope == "acceptance":
        command.extend(["-t", str(ACCEPTANCE_DURATION_SECONDS)])
    command.extend(
        [
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "segment",
            "-segment_time",
            str(config.segment_seconds),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "matroska",
            str(config.input_pattern),
        ]
    )
    return tuple(command)


def workflow_plan(config: PictureWorkflowConfig) -> dict[str, Any]:
    _validate_config(config)
    template_input = config.inputs_dir / "segment_0000.mkv"
    template_output = config.restored_dir / "segment_0000.restored.working.mkv"
    return {
        "purpose": "Jasna 纯画面 workflow；输出拼接好的完整视频轨，不处理音频、字幕或翻译",
        "config": config.as_dict(),
        "required_engines": [str(path) for path in required_engine_paths(config.jasna)],
        "prepare_command": list(build_prepare_command(config)),
        "jasna_segment_command_template": list(
            build_jasna_command(config.jasna, template_input, template_output)
        ),
        "gates": [
            "前级技术报告通过且用户已观看认可",
            "调用者确认 Whisper A/C 已退出并释放 GPU",
            "七个 TensorRT 引擎真实存在且非空",
            "30 分钟 acceptance 通过后才能进入 full",
        ],
    }


def _prepared_segment_record(config: PictureWorkflowConfig, path: Path) -> dict[str, Any]:
    if path.parent.resolve() != config.inputs_dir.resolve():
        raise ExperimentError(f"输入段不在当前 work_dir：{path}")
    probe = _probe(config.ffprobe, path)
    stream = _video_stream(probe)
    return {
        "path": str(path),
        "duration_seconds": _duration(probe),
        "stream_signature": list(_stream_signature(stream)),
    }


def _validate_prepared_segments(
    config: PictureWorkflowConfig,
    paths: Iterable[Path],
    expected_total_seconds: float,
) -> list[dict[str, Any]]:
    ordered = sorted(paths)
    if not ordered:
        raise ExperimentError("没有生成任何纯视频输入段。")
    expected_names = [f"segment_{index:04d}.mkv" for index in range(len(ordered))]
    if [path.name for path in ordered] != expected_names:
        raise ExperimentError("纯视频输入段序列不连续，拒绝恢复或继续。")
    if config.scope == "acceptance" and len(ordered) != 2:
        raise ExperimentError(
            f"30 分钟验收应生成两个约 15 分钟段，实际为 {len(ordered)} 个。"
        )
    records = [_prepared_segment_record(config, path) for path in ordered]
    first_signature = records[0]["stream_signature"]
    if any(record["stream_signature"] != first_signature for record in records[1:]):
        raise ExperimentError("流复制后的输入段视频参数不一致。")
    total = sum(float(record["duration_seconds"]) for record in records)
    if config.scope == "full":
        if abs(total - expected_total_seconds) > FULL_DURATION_TOLERANCE_SECONDS:
            raise ExperimentError(
                "整片纯视频输入段总量偏差明显："
                f"实际 {total:.3f}s，源片 {expected_total_seconds:.3f}s，"
                f"允许 {FULL_DURATION_TOLERANCE_SECONDS:.3f}s"
            )
    else:
        _assert_extracted_total(total, expected_total_seconds, "纯视频输入段总量")
    return records


def _source_video_origin(ffprobe: Path, source: Path) -> dict[str, Any]:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        "%+#64",
        "-show_entries",
        "packet=pts_time,flags",
        "-of",
        "json",
        str(source),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise ExperimentError(f"无法取得源片视频 packet PTS：{result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
        packets = payload["packets"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ExperimentError("源片没有可解析的视频 packet 时间轴。") from exc
    if not isinstance(packets, list) or not packets:
        raise ExperimentError("源片没有可用的视频 packet PTS。")

    first_packet_pts: float | None = None
    first_packet_flags = ""
    discarded_preroll_packets = 0
    for packet in packets:
        if not isinstance(packet, dict):
            raise ExperimentError("源片视频 packet 记录不是 JSON object。")
        flags = str(packet.get("flags", ""))
        try:
            pts = float(packet["pts_time"])
        except (KeyError, TypeError, ValueError):
            raise ExperimentError("源片视频 packet PTS 不是数值。")
        if not math.isfinite(pts):
            raise ExperimentError("源片视频 packet PTS 不是有限数。")
        if first_packet_pts is None:
            first_packet_pts = pts
            first_packet_flags = flags
        if "D" in flags:
            discarded_preroll_packets += 1
            continue
        if pts < 0:
            return {
                "source_origin_seconds": 0.0,
                "first_presentable_packet_pts_seconds": pts,
                "first_packet_pts_seconds": first_packet_pts,
                "first_packet_flags": first_packet_flags,
                "discarded_preroll_packets": discarded_preroll_packets,
                "informational_warnings": [
                    {
                        "code": "NEGATIVE_PRESENTABLE_ORIGIN_CLAMPED",
                        "message": (
                            "首个可呈现视频 packet 位于零点前 "
                            f"{abs(pts):.6f}s；已按 0 秒起点继续，"
                            "该容器级预卷偏差留给人工同步验收。"
                        ),
                    }
                ],
            }
        return {
            "source_origin_seconds": pts,
            "first_presentable_packet_pts_seconds": pts,
            "first_packet_pts_seconds": first_packet_pts,
            "first_packet_flags": first_packet_flags,
            "discarded_preroll_packets": discarded_preroll_packets,
            "informational_warnings": [],
        }
    raise ExperimentError("源片开头没有可用的非 discard 视频 packet PTS。")


def _successful_prepare_log(config: PictureWorkflowConfig) -> tuple[Path, float]:
    evidence = _successful_streamed_log(config.logs_dir, "prepare")
    if evidence is not None:
        return evidence
    raise ExperimentError(
        "发现没有 prepare report 的输入段，但没有匹配的成功抽取日志；"
        "保留现场并停止。"
    )


def _prepare_segments(
    config: PictureWorkflowConfig,
    expected_total_seconds: float,
    *,
    mirror_output: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if config.prepare_report_path.exists():
        report = _load_json(config.prepare_report_path, "prepare report")
        if report.get("status") != "prepared":
            raise ExperimentError(f"prepare report 状态无效：{config.prepare_report_path}")
        recorded = [Path(item["path"]) for item in report.get("segments", [])]
        actual = sorted(config.inputs_dir.glob("segment_*.mkv"))
        if recorded != actual:
            raise ExperimentError("prepare report 与当前输入段列表不一致，拒绝继续。")
        records = _validate_prepared_segments(config, actual, expected_total_seconds)
        if config.scope == "full" and report.get("source_origin_seconds") is None:
            raise ExperimentError("整片 prepare report 缺少源片首 packet 起点，拒绝猜测。")
        return records, {**report, "reused": True}

    existing = sorted(config.inputs_dir.glob("segment_*.mkv")) if config.inputs_dir.exists() else []
    if existing:
        records = _validate_prepared_segments(config, existing, expected_total_seconds)
        log_path, elapsed = _successful_prepare_log(config)
        origin_evidence = (
            _source_video_origin(config.ffprobe, config.source)
            if config.scope == "full"
            else None
        )
        report = {
            "status": "prepared",
            "elapsed_seconds": elapsed,
            "expected_total_seconds": expected_total_seconds,
            "actual_total_seconds": sum(
                float(item["duration_seconds"]) for item in records
            ),
            "segments": records,
            "source_origin_seconds": (
                origin_evidence["source_origin_seconds"]
                if origin_evidence is not None
                else None
            ),
            "source_origin_evidence": origin_evidence,
            "log": str(log_path),
            "reused": True,
            "recovered_unreported_segments": True,
        }
        _write_json(config.prepare_report_path, report)
        return records, report

    origin_evidence = (
        _source_video_origin(config.ffprobe, config.source)
        if config.scope == "full"
        else None
    )

    config.inputs_dir.mkdir(parents=True, exist_ok=True)
    log_path = _next_log_path(config.logs_dir, "prepare")
    prepare_timeout = max(300.0, expected_total_seconds * 0.2)
    elapsed = _run_streamed(
        build_prepare_command(config),
        log_path,
        mirror_output=mirror_output,
        timeout_seconds=prepare_timeout,
    )
    actual = sorted(config.inputs_dir.glob("segment_*.mkv"))
    records = _validate_prepared_segments(config, actual, expected_total_seconds)
    report = {
        "status": "prepared",
        "elapsed_seconds": elapsed,
        "expected_total_seconds": expected_total_seconds,
        "actual_total_seconds": sum(float(item["duration_seconds"]) for item in records),
        "segments": records,
        "source_origin_seconds": (
            origin_evidence["source_origin_seconds"]
            if origin_evidence is not None
            else None
        ),
        "source_origin_evidence": origin_evidence,
        "log": str(log_path),
        "reused": False,
        "recovered_unreported_segments": False,
    }
    _write_json(config.prepare_report_path, report)
    return records, report


def _require_hardware_decoder(log_path: Path) -> str:
    text = log_path.read_text(encoding="utf-8", errors="replace").casefold()
    if "using vali nvdec decoder" in text:
        return "vali-nvdec"
    if "using pyav cuda" in text:
        return "pyav-cuda"
    raise ExperimentError(
        "Jasna 日志没有出现 VALI NVDEC 或 PyAV CUDA 硬解码证据，拒绝把该段当成正式完成："
        f"{log_path}"
    )


def _validate_processed_pair(
    config: PictureWorkflowConfig,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    input_probe = _probe(config.ffprobe, input_path)
    output_probe = _probe(config.ffprobe, output_path)
    input_stream = _video_stream(input_probe)
    output_stream = _video_stream(output_probe)
    if output_stream.get("codec_name") != EXPECTED_OUTPUT_CODEC:
        raise ExperimentError(
            f"Jasna 段输出不是 {EXPECTED_OUTPUT_CODEC}：{output_path}"
        )
    input_shape = (
        input_stream.get("width"),
        input_stream.get("height"),
        input_stream.get("avg_frame_rate"),
    )
    output_shape = (
        output_stream.get("width"),
        output_stream.get("height"),
        output_stream.get("avg_frame_rate"),
    )
    if input_shape != output_shape:
        raise ExperimentError(f"段输入输出分辨率或平均帧率不一致：{output_path}")
    input_duration = _duration(input_probe)
    output_duration = _duration(output_probe)
    _assert_close_duration(output_duration, input_duration, f"段输出 {output_path.name}")
    return {
        "input": str(input_path),
        "output": str(output_path),
        "input_duration_seconds": input_duration,
        "output_duration_seconds": output_duration,
        "output_stream": output_stream,
    }


def _segment_paths(config: PictureWorkflowConfig, input_path: Path) -> tuple[Path, Path, Path]:
    stem = input_path.stem
    output = config.restored_dir / f"{stem}.restored.mkv"
    working = config.restored_dir / f"{stem}.restored.working.mkv"
    report = config.reports_dir / f"{stem}.json"
    return output, working, report


def _write_segment_report(
    report_path: Path,
    validation: dict[str, Any],
    *,
    elapsed_seconds: float | None,
    log_path: Path | None,
    decoder_backend: str | None,
    reused: bool,
    recovered_working_file: bool,
    preserved_interrupted_file: Path | None = None,
) -> dict[str, Any]:
    report = {
        "status": "technical_checks_passed",
        **validation,
        "elapsed_seconds": elapsed_seconds,
        "log": str(log_path) if log_path is not None else None,
        "decoder_backend": decoder_backend,
        "reused": reused,
        "recovered_working_file": recovered_working_file,
        "preserved_interrupted_file": (
            str(preserved_interrupted_file)
            if preserved_interrupted_file is not None
            else None
        ),
    }
    _write_json(report_path, report)
    return report


def _process_segment(
    config: PictureWorkflowConfig,
    input_path: Path,
    *,
    mirror_output: bool = True,
    idle_gpu_threshold_percent: int = IDLE_GPU_THRESHOLD_PERCENT,
) -> dict[str, Any]:
    output, working, report_path = _segment_paths(config, input_path)
    if output.exists():
        validation = _validate_processed_pair(config, input_path, output)
        if not report_path.exists():
            evidence = _successful_streamed_log(
                config.logs_dir, f"process-{input_path.stem}"
            )
            if evidence is None:
                raise ExperimentError(
                    "发现没有完成报告的段输出，且没有成功处理日志："
                    f"{output}"
                )
            log_path, elapsed = evidence
            decoder_backend = _require_hardware_decoder(log_path)
            return _write_segment_report(
                report_path,
                validation,
                elapsed_seconds=elapsed,
                log_path=log_path,
                decoder_backend=decoder_backend,
                reused=True,
                recovered_working_file=True,
            )
        report = _load_json(report_path, "segment report")
        if report.get("status") != "technical_checks_passed":
            raise ExperimentError(f"完成段报告状态无效：{report_path}")
        if report.get("decoder_backend") not in {"vali-nvdec", "pyav-cuda"}:
            raise ExperimentError(f"完成段报告缺少硬解码证据：{report_path}")
        return {**report, **validation, "reused": True}

    preserved_interrupted_file: Path | None = None
    if working.exists():
        evidence = _successful_streamed_log(
            config.logs_dir, f"process-{input_path.stem}"
        )
        if evidence is not None:
            log_path, elapsed = evidence
            try:
                decoder_backend = _require_hardware_decoder(log_path)
                validation = _validate_processed_pair(config, input_path, working)
            except ExperimentError:
                pass
            else:
                working.replace(output)
                validation["output"] = str(output)
                return _write_segment_report(
                    report_path,
                    validation,
                    elapsed_seconds=elapsed,
                    log_path=log_path,
                    decoder_backend=decoder_backend,
                    reused=True,
                    recovered_working_file=True,
                )
        preserved_interrupted_file = _preserve_interrupted_file(working)

    config.restored_dir.mkdir(parents=True, exist_ok=True)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    duration = _duration(_probe(config.ffprobe, input_path))
    timeout_seconds = max(180.0, duration * SEGMENT_TIMEOUT_REALTIME_FACTOR)
    log_path = _next_log_path(config.logs_dir, f"process-{input_path.stem}")
    elapsed = _run_streamed(
        build_jasna_command(config.jasna, input_path, working),
        log_path,
        mirror_output=mirror_output,
        stop_on_software_decode=True,
        timeout_seconds=timeout_seconds,
        progress_stall_seconds=SEGMENT_STALL_SECONDS,
        idle_gpu_threshold_percent=idle_gpu_threshold_percent,
    )
    decoder_backend = _require_hardware_decoder(log_path)
    validation = _validate_processed_pair(config, input_path, working)
    working.replace(output)
    validation["output"] = str(output)
    return _write_segment_report(
        report_path,
        validation,
        elapsed_seconds=elapsed,
        log_path=log_path,
        decoder_backend=decoder_backend,
        reused=False,
        recovered_working_file=False,
        preserved_interrupted_file=preserved_interrupted_file,
    )


def _ffconcat_line(path: Path) -> str:
    normalized = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{normalized}'"


def _write_concat_manifest(path: Path, segments: Iterable[Path]) -> None:
    path.write_text(
        "\n".join(_ffconcat_line(segment) for segment in segments) + "\n",
        encoding="utf-8",
    )


def _final_working_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.working{output.suffix}")


def _validate_final(
    config: PictureWorkflowConfig,
    final_path: Path,
    segment_reports: list[dict[str, Any]],
    expected_total_seconds: float,
    verify_log: Path,
    source_origin_seconds: float | None,
) -> dict[str, Any]:
    final_probe = _probe(config.ffprobe, final_path)
    final_stream = _video_stream(final_probe)
    if final_stream.get("codec_name") != EXPECTED_OUTPUT_CODEC:
        raise ExperimentError(f"拼接成品不是 {EXPECTED_OUTPUT_CODEC}：{final_path}")
    segment_signatures = [
        _stream_signature(report["output_stream"]) for report in segment_reports
    ]
    if not segment_signatures or any(
        signature != segment_signatures[0] for signature in segment_signatures[1:]
    ):
        raise ExperimentError("待拼接段的视频编码参数不一致。")
    if _stream_signature(final_stream) != segment_signatures[0]:
        raise ExperimentError("拼接成品与完成段的视频编码参数不一致。")

    segment_durations = [
        float(report["output_duration_seconds"]) for report in segment_reports
    ]
    segment_sum = sum(segment_durations)
    final_duration = _duration(final_probe)
    _assert_close_duration(final_duration, segment_sum, "拼接成品")
    if config.scope == "full":
        if abs(final_duration - expected_total_seconds) > FULL_DURATION_TOLERANCE_SECONDS:
            raise ExperimentError(
                "整片拼接成品时长偏差明显："
                f"实际 {final_duration:.3f}s，源片 {expected_total_seconds:.3f}s，"
                f"允许 {FULL_DURATION_TOLERANCE_SECONDS:.3f}s"
            )
    else:
        _assert_extracted_total(final_duration, expected_total_seconds, "拼接成品总量")

    source_cursor = source_origin_seconds
    output_cursor = 0.0
    segment_mappings: list[dict[str, Any]] = []
    for index, report in enumerate(segment_reports):
        input_duration = float(report["input_duration_seconds"])
        output_duration = float(report["output_duration_seconds"])
        mapping = {
            "index": index,
            "input": report["input"],
            "output": report["output"],
            "input_duration_seconds": input_duration,
            "output_start_seconds": round(output_cursor, 6),
            "output_end_seconds": round(output_cursor + output_duration, 6),
        }
        if source_cursor is not None:
            mapping.update(
                {
                    "source_start_seconds": round(source_cursor, 6),
                    "source_end_seconds": round(source_cursor + input_duration, 6),
                }
            )
            source_cursor += input_duration
        segment_mappings.append(mapping)
        output_cursor += output_duration
    output_seams = [
        mapping["output_end_seconds"] for mapping in segment_mappings[:-1]
    ]

    positions: list[tuple[str, float]] = [("start", 0.0)]
    cumulative = 0.0
    for index, duration in enumerate(segment_durations[:-1], start=1):
        cumulative += duration
        positions.append((f"seam_{index:04d}", max(0.0, cumulative - 0.5)))
    positions.append(("end", max(0.0, final_duration - 1.0)))
    decode_spots = _decode_spots(config.ffmpeg, final_path, positions, verify_log)
    return {
        "status": "technical_checks_passed",
        "scope": config.scope,
        "technical_scope": "纯视频轨、段时长、整片总量、编码参数与接缝附近解码；画质仍需用户观看",
        "output": str(config.output),
        "final_duration_seconds": final_duration,
        "expected_total_seconds": expected_total_seconds,
        "segment_count": len(segment_reports),
        "segment_sum_seconds": segment_sum,
        "source_identity": _source_identity(config),
        "source_origin_seconds": source_origin_seconds,
        "segment_mappings": segment_mappings,
        "output_seam_positions_seconds": output_seams,
        "route": _frozen_route(),
        "workflow_manifest": str(config.manifest_path),
        "prepare_report": str(config.prepare_report_path),
        "final_video_stream": final_stream,
        "decode_spots": decode_spots,
    }


def _concat_and_verify(
    config: PictureWorkflowConfig,
    segment_reports: list[dict[str, Any]],
    expected_total_seconds: float,
    source_origin_seconds: float | None,
    *,
    mirror_output: bool = True,
) -> dict[str, Any]:
    outputs = [Path(report["output"]) for report in segment_reports]
    if not outputs:
        raise ExperimentError("没有可拼接的完成段。")
    for output in outputs:
        _require_file(output, "完成段")

    config.output.parent.mkdir(parents=True, exist_ok=True)
    working = _final_working_path(config.output)
    verify_log = _next_log_path(config.logs_dir, "verify-final")

    if config.output.exists():
        if not config.final_report_path.exists():
            report = _validate_final(
                config,
                config.output,
                segment_reports,
                expected_total_seconds,
                verify_log,
                source_origin_seconds,
            )
            report["reused"] = True
            report["recovered_working_file"] = True
            _write_json(config.final_report_path, report)
            return report
        existing_report = _load_json(config.final_report_path, "workflow report")
        if existing_report.get("status") != "technical_checks_passed":
            raise ExperimentError(f"最终技术报告状态无效：{config.final_report_path}")
        report = _validate_final(
            config,
            config.output,
            segment_reports,
            expected_total_seconds,
            verify_log,
            source_origin_seconds,
        )
        report["reused"] = True
        _write_json(config.final_report_path, report)
        return report

    preserved_interrupted_file: Path | None = None
    if working.exists():
        try:
            report = _validate_final(
                config,
                working,
                segment_reports,
                expected_total_seconds,
                verify_log,
                source_origin_seconds,
            )
        except ExperimentError:
            preserved_interrupted_file = _preserve_interrupted_file(working)
        else:
            working.replace(config.output)
            report["output"] = str(config.output)
            report["reused"] = True
            report["recovered_working_file"] = True
            _write_json(config.final_report_path, report)
            return report

    _write_concat_manifest(config.concat_manifest_path, outputs)
    concat_command = (
        str(config.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-n",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(config.concat_manifest_path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        str(working),
    )
    concat_log = _next_log_path(config.logs_dir, "concat")
    concat_elapsed = _run_streamed(
        concat_command,
        concat_log,
        mirror_output=mirror_output,
        timeout_seconds=600.0,
    )
    report = _validate_final(
        config,
        working,
        segment_reports,
        expected_total_seconds,
        verify_log,
        source_origin_seconds,
    )
    working.replace(config.output)
    report["output"] = str(config.output)
    report["concat_elapsed_seconds"] = concat_elapsed
    report["concat_log"] = str(concat_log)
    report["reused"] = False
    report["recovered_working_file"] = False
    report["preserved_interrupted_file"] = (
        str(preserved_interrupted_file)
        if preserved_interrupted_file is not None
        else None
    )
    _write_json(config.final_report_path, report)
    return report


def run_picture_workflow(
    config: PictureWorkflowConfig,
    *,
    confirm_gpu_ready: bool = False,
    confirm_gate_viewed: bool = False,
    mirror_output: bool = True,
    cancelled=None,
    on_progress=None,
) -> dict[str, Any]:
    cancelled = cancelled or (lambda: False)
    check_cancelled(cancelled)
    gate_report = _require_gate(
        config,
        confirm_gpu_ready=confirm_gpu_ready,
        confirm_gate_viewed=confirm_gate_viewed,
    )
    preflight = preflight_workflow(config)
    gpu_background_percent = _gpu_utilization_percent()
    idle_gpu_threshold_percent = (
        min(100, gpu_background_percent + 10)
        if gpu_background_percent is not None
        else IDLE_GPU_THRESHOLD_PERCENT
    )
    manifest = _load_or_create_manifest(config)
    expected_total = (
        float(ACCEPTANCE_DURATION_SECONDS)
        if config.scope == "acceptance"
        else float(preflight["source_duration_seconds"])
    )
    prepared_records, prepare_report = _prepare_segments(
        config,
        expected_total,
        mirror_output=mirror_output,
    )
    segment_reports = []
    for record in prepared_records:
        check_cancelled(cancelled)
        segment_reports.append(_process_segment(
            config,
            Path(record["path"]),
            mirror_output=mirror_output,
            idle_gpu_threshold_percent=idle_gpu_threshold_percent,
        ))
        if on_progress:
            on_progress(len(segment_reports), len(prepared_records))
    check_cancelled(cancelled)
    final_report = _concat_and_verify(
        config,
        segment_reports,
        expected_total,
        prepare_report.get("source_origin_seconds"),
        mirror_output=mirror_output,
    )
    origin_evidence = prepare_report.get("source_origin_evidence", {})
    if isinstance(origin_evidence, dict):
        final_report["informational_warnings"] = list(
            origin_evidence.get("informational_warnings", [])
        )
    final_report = _persist_global_route_gate_evidence(
        config,
        final_report,
        preflight.get("gate_lineage"),
    )
    return {
        **final_report,
        "workflow_manifest": str(config.manifest_path),
        "workflow_config": manifest,
        "gate_report": str(config.gate_report),
        "gate_status": gate_report.get("status"),
        "preflight": preflight,
        "gpu_background_percent": gpu_background_percent,
        "idle_gpu_threshold_percent": idle_gpu_threshold_percent,
        "prepare": prepare_report,
        "segments": segment_reports,
        "report": str(config.final_report_path),
    }
