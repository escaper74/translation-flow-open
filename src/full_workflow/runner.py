from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Mapping

from final_mux import FinalMuxConfig, FinalMuxWorkflow
from jasna_picture import PictureWorkflowConfig, run_picture_workflow
from media_planning import MediaRepackageRequest, MediaRepackageRunner, OutputTarget
from translation_flow.events import EventEmitter
from translation_flow.contracts import ApiProviderRequest
from translation_flow.packaging import validate_completed_mkv
from translation_flow.runner import TranslationFlowRunner, _safe_stem, _validate_api_translation
from translation_flow.srt import parse_srt, subtitle_digest, validate_timeline
from translation_flow.translation.codex_provider import TranslationCancelled
from translation_flow.runtime import DEFAULT_TOOL_ROOT, tool_root as resolve_tool_root, jasna_path, acceptance_report
from translation_flow.cancellation import check_cancelled

from .contracts import FullWorkflowRequest


DEFAULT_JASNA = DEFAULT_TOOL_ROOT / "jasna" / "jasna.exe"
DEFAULT_ACCEPTANCE_GATE_REPORT = DEFAULT_TOOL_ROOT / "acceptance" / "workflow-report.json"
REPORT_TIME_TOLERANCE_SECONDS = Decimal("0.05")
REPORT_DURATION_TOLERANCE_SECONDS = Decimal("2.0")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON object：{path}")
    return value


def _resolved(value: object) -> Path:
    return Path(str(value)).resolve()


def _same_decimal(
    value: object,
    expected: object,
    *,
    tolerance: Decimal = REPORT_TIME_TOLERANCE_SECONDS,
) -> bool:
    try:
        actual_decimal = Decimal(str(value))
        expected_decimal = Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False
    return (
        actual_decimal.is_finite()
        and expected_decimal.is_finite()
        and abs(actual_decimal - expected_decimal) <= tolerance
    )


@dataclass(frozen=True)
class FullWorkflowPaths:
    workflow_root: Path
    processed_video: Path
    final_output: Path

    @property
    def manifest(self) -> Path:
        return self.workflow_root / "workflow.json"

    @property
    def result(self) -> Path:
        return self.workflow_root / "workflow.result.json"

    @property
    def picture_work_dir(self) -> Path:
        return self.workflow_root / "picture"

    @property
    def final_mux_work_dir(self) -> Path:
        return self.workflow_root / "final-mux"


class _TranslationEventRelay:
    """Keep the translation record while forwarding progress to the top-level job."""

    def __init__(self, local: EventEmitter, top: EventEmitter) -> None:
        self.local = local
        self.top = top

    def emit(self, event: str, **payload: Any) -> None:
        self.local.emit(event, **payload)
        if event == "job_started":
            self.top.emit(
                "stage_started",
                stage="translation_flow",
                overall_percent=1,
                detail="开始翻译主线。",
            )
        elif event == "job_completed":
            self.top.emit(
                "stage_completed",
                stage="translation_flow",
                overall_percent=60,
                outputs=payload.get("outputs", {}),
            )
        else:
            forwarded = dict(payload)
            overall = forwarded.get("overall_percent")
            if isinstance(overall, (int, float)):
                forwarded["overall_percent"] = round(min(60.0, overall * 0.6), 1)
            self.top.emit(event, **forwarded)


class _SilentTextStream:
    """Discard nested workflow stdout so the desktop JSONL channel stays valid."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


TranslationRunnerFactory = Callable[..., Any]
PictureRunner = Callable[..., dict[str, Any]]
FinalMuxFactory = Callable[..., Any]
MediaDeliveryFactory = Callable[..., Any]


def validate_picture_result(result: Mapping[str, Any], source: Path, processed_video: Path) -> dict[str, Any]:
    if result.get("status") != "technical_checks_passed" or result.get("scope") != "full":
        raise ValueError("整片画面 workflow 没有交付 technical_checks_passed/full 报告")
    if _resolved(result.get("output", "")) != processed_video:
        raise ValueError("整片画面报告的输出与顶层 workflow 不匹配")
    if not processed_video.is_file():
        raise FileNotFoundError(f"整片画面产物不存在：{processed_video}")
    source_identity = result.get("source_identity", {})
    stat = source.stat()
    if not isinstance(source_identity, Mapping) or (
        _resolved(source_identity.get("path", "")) != source.resolve()
        or source_identity.get("size") != stat.st_size
        or source_identity.get("mtime_ns") != stat.st_mtime_ns
    ):
        raise ValueError("整片画面报告的源片谱系与本次任务不匹配")
    try:
        source_origin = Decimal(str(result.get("source_origin_seconds")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("整片画面报告缺少可用的源片首 packet 起点") from exc
    if not source_origin.is_finite() or source_origin < 0:
        raise ValueError("整片画面报告没有证明从原片开头的首个视频 packet 开始")
    mappings = result.get("segment_mappings")
    seams = result.get("output_seam_positions_seconds")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("整片画面报告缺少 segment_mappings")
    if not isinstance(seams, list):
        raise ValueError("整片画面报告缺少 output_seam_positions_seconds")
    if not isinstance(result.get("route"), Mapping):
        raise ValueError("整片画面报告缺少冻结处理路线")
    picture_report_path = Path(str(result.get("report", "")))
    if not picture_report_path.is_file():
        raise FileNotFoundError(f"整片画面正式报告不存在：{picture_report_path}")

    output_cursor = Decimal("0")
    source_cursor = source_origin
    expected_seams: list[Decimal] = []
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, Mapping) or mapping.get("index") != index:
            raise ValueError("整片画面 segment_mappings 顺序或结构无效")
        try:
            source_start = Decimal(str(mapping["source_start_seconds"]))
            source_end = Decimal(str(mapping["source_end_seconds"]))
            output_start = Decimal(str(mapping["output_start_seconds"]))
            output_end = Decimal(str(mapping["output_end_seconds"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError("整片画面 segment_mappings 缺少可用时间映射") from exc
        if (
            not all(
                value.is_finite()
                for value in (source_start, source_end, output_start, output_end)
            )
            or not _same_decimal(source_start, source_cursor)
            or not _same_decimal(output_start, output_cursor)
            or source_end <= source_start
            or output_end <= output_start
        ):
            raise ValueError("整片画面 segment_mappings 不连续或时长无效")
        source_cursor = source_end
        output_cursor = output_end
        if index < len(mappings) - 1:
            expected_seams.append(output_end)
    try:
        actual_seams = [Decimal(str(value)) for value in seams]
        final_duration = Decimal(str(result["final_duration_seconds"]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("整片画面报告缺少可用接缝或成品时长") from exc
    seams_match = len(actual_seams) == len(expected_seams) and all(
        _same_decimal(actual, expected)
        for actual, expected in zip(actual_seams, expected_seams)
    )
    if (
        not final_duration.is_finite()
        or not seams_match
        or abs(final_duration - output_cursor)
        > REPORT_DURATION_TOLERANCE_SECONDS
    ):
        raise ValueError("整片画面报告的接缝或成品时长与分段映射不匹配")
    return dict(result)


class FullWorkflowRunner:
    """Join translation and picture branches without owning their business."""

    def __init__(
        self,
        request: FullWorkflowRequest,
        api_key: str,
        *,
        project_root: str | Path | None = None,
        tool_root: str | Path | None = None,
        python: str | Path | None = None,
        jasna: str | Path | None = None,
        acceptance_gate_report: str | Path | None = None,
        translation_runner_factory: TranslationRunnerFactory = TranslationFlowRunner,
        picture_runner: PictureRunner = run_picture_workflow,
        final_mux_factory: FinalMuxFactory = FinalMuxWorkflow,
        media_delivery_factory: MediaDeliveryFactory = MediaRepackageRunner,
        emitter: EventEmitter | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.request = request
        self.cancelled = cancelled or (lambda: False)
        self.api_key = api_key
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2])
        self.tool_root = resolve_tool_root(tool_root, project_root=self.project_root)
        self.python = Path(python or sys.executable)
        self.jasna = jasna_path(jasna, tools=self.tool_root)
        self.acceptance_gate_report = acceptance_report(acceptance_gate_report, tools=self.tool_root)
        self.translation_runner_factory = translation_runner_factory
        self.picture_runner = picture_runner
        self.final_mux_factory = final_mux_factory
        self.media_delivery_factory = media_delivery_factory
        self.ffmpeg = (
            self.tool_root
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
            / "ffmpeg.exe"
        )
        self.ffprobe = self.ffmpeg.with_name("ffprobe.exe")

        translation = request.translation
        stem = _safe_stem(translation.input_video.stem)
        profile = translation.profile_id.replace("_", "-")
        run_label = _safe_stem(translation.job_id)[:12]
        workflow_root = (
            translation.output_dir / ".full-workflow" / translation.job_id
        ).resolve()
        self.paths = FullWorkflowPaths(
            workflow_root=workflow_root,
            processed_video=(
                translation.output_dir
                / f"{stem}.{profile}.{run_label}.restored.video-only.mkv"
            ).resolve(),
            final_output=(
                translation.output_dir
                / f"{stem}.{profile}.{run_label}.restored.zh-CN.mkv"
            ).resolve(),
        )
        self.events = emitter or EventEmitter(
            translation.job_id, workflow_root / "events.jsonl"
        )

    def _manifest_payload(self) -> dict[str, Any]:
        translation = self.request.translation
        source_stat = translation.input_video.stat()
        return {
            "schema_version": 1,
            "job_id": translation.job_id,
            "source": {
                "path": str(translation.input_video),
                "size": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            },
            "output_dir": str(translation.output_dir),
            "profile_id": translation.profile_id,
            "provider": vars(translation.provider),
            **({"asr_a_model": translation.asr_a_model} if translation.asr_a_model != "large-v3" else {}),
            "gates": {
                "subtitle_text_external_current_run": True,
                "picture_and_mux_30m_human_accepted": (
                    self.request.gates.picture_and_mux_30m_human_accepted
                ),
                "picture_gpu_after_asr_fusion": True,
            },
            "picture": {
                "scope": "full",
                "acceptance_gate_report": str(self.acceptance_gate_report.resolve()),
                "output": str(self.paths.processed_video),
            },
            "final_mux": {
                "source_origin_from_picture_report": True,
                "seams_from_picture_report": True,
                "output": str(self.paths.final_output),
            },
        }

    def _load_or_create_manifest(self) -> dict[str, Any]:
        expected = self._manifest_payload()
        if self.paths.manifest.exists():
            actual = _load_json(self.paths.manifest, "顶层 workflow manifest")
            comparable = dict(actual)
            comparable.pop("status", None)
            comparable.pop("error", None)
            actual_gates = dict(comparable.get("gates", {}))
            expected_gates = dict(expected.get("gates", {}))
            # Scheduling changes wall-clock behavior, not result lineage. Accept
            # the former serial gate so an old checkpoint can resume under the
            # new A+C branch schedule, then rewrite the manifest on completion.
            actual_gates.pop("picture_gpu_after_translation_completion", None)
            actual_gates.pop("picture_gpu_after_asr_fusion", None)
            expected_gates.pop("picture_gpu_after_translation_completion", None)
            expected_gates.pop("picture_gpu_after_asr_fusion", None)
            comparable["gates"] = actual_gates
            expected_comparable = {**expected, "gates": expected_gates}
            if comparable != expected_comparable:
                raise ValueError("顶层 workflow 目录与本次源片、模式或产物不匹配")
            return expected
        existing = (
            [
                path
                for path in self.paths.workflow_root.iterdir()
                if path.name not in {"request.json", "events.jsonl", "queue.run.lock", "cancel.requested"}
            ]
            if self.paths.workflow_root.exists()
            else []
        )
        if existing:
            raise FileExistsError("顶层 workflow 目录非空且没有 manifest，保留现场并停止")
        self.paths.workflow_root.mkdir(parents=True, exist_ok=True)
        _write_json(self.paths.manifest, expected)
        return expected

    def _translation_result_path(self) -> Path:
        translation = self.request.translation
        return (
            translation.output_dir
            / ".translation-flow"
            / translation.job_id
            / "job.result.json"
        )

    def _check_cancelled(self) -> None:
        check_cancelled(self._stopped)

    def _stopped(self) -> bool:
        return (self.cancelled() or (self.paths.workflow_root / "cancel.requested").exists()
                or (self._translation_result_path().parent / "cancel.requested").exists())

    def _validate_translation_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        outputs = result.get("outputs", {})
        technical = result.get("technical_validation", {})
        if not isinstance(outputs, Mapping) or not isinstance(technical, Mapping):
            raise ValueError("翻译结果缺少正式产物或技术验证字段")
        required = {
            "chinese_srt": outputs.get("chinese_srt"),
            "final_mkv": outputs.get("final_mkv"),
            "technical_report": outputs.get("technical_report"),
            "technical_report_reference": technical.get("report_path"),
        }
        if outputs.get("subtitle_review_md") is not None:
            required["subtitle_review_md"] = outputs.get("subtitle_review_md")
        if technical.get("status") != "passed":
            raise ValueError("翻译结果的技术验证未通过")
        for label, raw in required.items():
            if not raw or not Path(str(raw)).is_file():
                raise FileNotFoundError(f"翻译结果缺少 {label}：{raw}")
        job_root = self._translation_result_path().parent
        translation_report = _load_json(
            job_root / "translation.report.json", "翻译报告"
        )
        translation = self.request.translation
        if (
            translation_report.get("status") != "passed"
            or translation_report.get("profile") != translation.profile_id
            or translation_report.get("provider") != translation.provider.id
            or translation_report.get("model") != translation.provider.model
            or _resolved(translation_report.get("output", ""))
            != _resolved(outputs["chinese_srt"])
        ):
            raise ValueError("翻译报告的 profile/provider/model/产物与本次任务不匹配")
        technical_report = _load_json(
            Path(str(outputs["technical_report"])), "翻译封装技术报告"
        )
        if (
            technical_report.get("status") != "passed"
            or _resolved(technical_report.get("source", ""))
            != translation.input_video.resolve()
            or _resolved(technical_report.get("chinese_srt", ""))
            != _resolved(outputs["chinese_srt"])
            or _resolved(technical_report.get("output", ""))
            != _resolved(outputs["final_mkv"])
        ):
            raise ValueError("翻译封装技术报告的输入输出谱系不匹配")
        asr = result.get("asr", {})
        source = translation.input_video
        source_stat = source.stat()
        if not isinstance(asr, Mapping) or (
            _resolved(asr.get("source", "")) != source.resolve()
            or asr.get("source_size_bytes") != source_stat.st_size
            or asr.get("source_mtime_ns") != source_stat.st_mtime_ns
            or asr.get("a_model", "large-v3") != translation.asr_a_model
        ):
            raise ValueError("翻译结果的 ASR 源片谱系与本次任务不匹配")
        fusion_srt = Path(str(asr.get("fusion_srt", "")))
        if not fusion_srt.is_file():
            raise FileNotFoundError(f"翻译结果缺少正式 ASR fusion SRT：{fusion_srt}")
        fusion_cues = parse_srt(fusion_srt)
        translation_source = fusion_cues
        if translation.provider.id == "codex":
            from translation_flow.codex_runner import validate_codex_translation
            reviewed, _, source_review, _ = validate_codex_translation(
                translation, job_root, Path(str(outputs["chinese_srt"])),
            )
            if (_resolved(outputs.get("reviewed_japanese_srt", "")) != reviewed.resolve()
                    or _resolved(outputs.get("source_review_report", "")) != _resolved(source_review["report_path"])):
                raise ValueError("Codex 结果的日文参考或源文审查报告不匹配")
            translation_source = parse_srt(reviewed)
        chinese_cues = parse_srt(Path(str(outputs["chinese_srt"])))
        validate_timeline(fusion_cues, chinese_cues)
        if (
            translation_report.get("source_sha256") != subtitle_digest(translation_source)
            or translation_report.get("subtitle_count") != len(fusion_cues)
            or translation_report.get("translated_count") != len(chinese_cues)
            or asr.get("fusion_cues") != len(fusion_cues)
        ):
            raise ValueError("翻译结果的 fusion/中文字幕结构谱系与正式报告不匹配")
        packaged_cues = technical_report.get("input_subtitle_cues")
        if packaged_cues is not None and packaged_cues != len(chinese_cues):
            raise ValueError("翻译封装报告的字幕数量与中文字幕不匹配")
        if isinstance(translation.provider, ApiProviderRequest):
            _validate_api_translation(
                translation, asr, fusion_cues, chinese_cues, translation_report,
                Path(str(outputs["chinese_srt"])),
            )
        if isinstance(translation.provider, ApiProviderRequest) or translation.provider.id == "codex":
            validate_completed_mkv(
                Path(str(outputs["final_mkv"])), Path(str(outputs["technical_report"])),
                source, Path(str(outputs["chinese_srt"])),
            )
        return dict(result)

    def _run_or_reuse_translation(
        self,
        *,
        on_asr_ready: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        result_path = self._translation_result_path()
        provider_label = "DeepSeek" if self.request.translation.provider.id == "deepseek" else self.request.translation.provider.id
        if result_path.is_file():
            try:
                return self._validate_translation_result(
                    _load_json(result_path, "翻译完成报告")
                ), True
            except Exception as exc:
                if not self.api_key and self.request.translation.provider.id != "codex":
                    raise ValueError(
                        f"缺少本次 {provider_label} API Key，且现有正式翻译结果"
                        "未通过谱系验证，无法复用"
                    ) from exc
                raise

        if not self.api_key and self.request.translation.provider.id == "deepseek":
            raise ValueError("缺少本次 DeepSeek API Key，且没有可复用的正式翻译结果")

        translation = self.request.translation
        local_events = EventEmitter(
            translation.job_id,
            result_path.parent / "events.jsonl",
            stdout=_SilentTextStream(),
        )
        relay = _TranslationEventRelay(local_events, self.events)
        runner = self.translation_runner_factory(
            translation,
            self.api_key,
            project_root=self.project_root,
            tool_root=self.tool_root,
            python=self.python,
            emitter=relay,
            on_asr_ready=on_asr_ready,
            cancelled=self._stopped,
        )
        result = runner.run()
        return self._validate_translation_result(result), False

    def _picture_config(self) -> PictureWorkflowConfig:
        return PictureWorkflowConfig(
            scope="full",
            source=self.request.translation.input_video,
            work_dir=self.paths.picture_work_dir,
            output=self.paths.processed_video,
            jasna=self.jasna,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            gate_report=self.acceptance_gate_report,
            start=None,
        )

    def _validate_picture_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        return validate_picture_result(result, self.request.translation.input_video, self.paths.processed_video)

    def _final_config(
        self,
        translation_result: Mapping[str, Any],
        picture_result: Mapping[str, Any],
    ) -> FinalMuxConfig:
        outputs = translation_result["outputs"]
        return FinalMuxConfig(
            processed_video=self.paths.processed_video,
            source_media=self.request.translation.input_video,
            chinese_srt=Path(str(outputs["chinese_srt"])),
            source_start_seconds=str(picture_result["source_origin_seconds"]),
            output=self.paths.final_output,
            work_dir=self.paths.final_mux_work_dir,
            seam_positions_seconds=tuple(
                str(item) for item in picture_result["output_seam_positions_seconds"]
            ),
        )

    def _validate_final_result(
        self,
        report: Mapping[str, Any],
        config: FinalMuxConfig,
    ) -> dict[str, Any]:
        lineage = report.get("input_lineage", {})
        technical = report.get("technical_validation", {})
        if report.get("status") != "technical_checks_passed":
            raise ValueError("最终汇合报告未通过技术验收")
        if not isinstance(lineage, Mapping) or (
            _resolved(lineage.get("processed_video", "")) != config.processed_video.resolve()
            or _resolved(lineage.get("source_media", "")) != config.source_media.resolve()
            or _resolved(lineage.get("full_timeline_chinese_srt", ""))
            != config.chinese_srt.resolve()
        ):
            raise ValueError("最终汇合报告的输入谱系与本次任务不匹配")
        if not _same_decimal(report.get("source_start_seconds"), config.source_start_seconds):
            raise ValueError("最终汇合报告的源起点与画面报告不匹配")
        report_seams = report.get("seam_positions_seconds")
        if not isinstance(report_seams, list) or len(report_seams) != len(
            config.seam_positions_seconds
        ) or any(
            not _same_decimal(actual, expected)
            for actual, expected in zip(report_seams, config.seam_positions_seconds)
        ):
            raise ValueError("最终汇合报告的接缝位置与画面报告不匹配")
        if _resolved(report.get("output", "")) != config.output.resolve():
            raise ValueError("最终汇合报告的输出与顶层 workflow 不匹配")
        if not config.output.is_file():
            raise FileNotFoundError(f"最终成片不存在：{config.output}")
        checks = technical.get("checks", {}) if isinstance(technical, Mapping) else {}
        if (
            not isinstance(checks, Mapping)
            or not checks
            or not all(value is True for value in checks.values())
        ):
            raise ValueError("最终汇合报告的技术检查不完整")
        return dict(report)

    def _run_or_reuse_final_mux(
        self,
        config: FinalMuxConfig,
    ) -> tuple[dict[str, Any], bool]:
        if config.output.is_file() and config.report_path.is_file():
            report = _load_json(config.report_path, "最终汇合报告")
            return self._validate_final_result(report, config), True
        if config.output.exists() or config.report_path.exists():
            raise FileExistsError("最终汇合只存在部分终态，保留现场并停止")
        workflow = self.final_mux_factory(self.ffmpeg, self.ffprobe)
        report = workflow.run(config)
        return self._validate_final_result(report, config), False

    def _mp4_delivery_request(self) -> MediaRepackageRequest:
        translation = self.request.translation
        return MediaRepackageRequest(
            schema_version=1,
            job_id=translation.job_id,
            source_media=self.paths.final_output,
            output_dir=translation.output_dir,
            output_target=OutputTarget.MP4,
        )

    def _validate_mp4_delivery_result(
        self,
        report: Mapping[str, Any],
        request: MediaRepackageRequest,
    ) -> dict[str, Any]:
        if report.get("status") != "technical_checks_passed":
            raise ValueError("追加 MP4 报告没有通过技术检查")
        source = report.get("source", {})
        output = report.get("output", {})
        technical = report.get("technical_validation", {})
        if not isinstance(source, Mapping) or not isinstance(output, Mapping):
            raise ValueError("追加 MP4 报告缺少输入输出谱系")
        source_stat = request.source_media.stat()
        if (
            _resolved(source.get("path", "")) != request.source_media.resolve()
            or source.get("size_bytes") != source_stat.st_size
            or source.get("mtime_ns") != source_stat.st_mtime_ns
        ):
            raise ValueError("追加 MP4 报告的 MKV 源谱系与本次任务不匹配")
        if (
            _resolved(output.get("path", "")) != request.output_path
            or output.get("target") != OutputTarget.MP4.value
            or not request.output_path.is_file()
        ):
            raise ValueError("追加 MP4 报告的输出与本次任务不匹配")
        checks = technical.get("checks", {}) if isinstance(technical, Mapping) else {}
        if (
            not isinstance(checks, Mapping)
            or not checks
            or not all(value is True for value in checks.values())
        ):
            raise ValueError("追加 MP4 报告的技术检查不完整")
        return dict(report)

    def _run_or_reuse_mp4_delivery(
        self,
    ) -> tuple[dict[str, Any], MediaRepackageRequest, bool]:
        request = self._mp4_delivery_request()
        output = request.output_path
        report_path = request.technical_report_path
        if output.is_file() and report_path.is_file():
            report = _load_json(report_path, "追加 MP4 技术报告")
            return self._validate_mp4_delivery_result(report, request), request, True
        if output.exists() or report_path.exists():
            raise FileExistsError("追加 MP4 只存在部分终态；正式 MKV 保留，MP4 停止")
        local_events = EventEmitter(
            request.job_id,
            request.job_root / "events.jsonl",
            stdout=_SilentTextStream(),
        )
        runner = self.media_delivery_factory(
            request,
            tool_root=self.tool_root,
            emitter=local_events,
        )
        runner.run()
        report = _load_json(report_path, "追加 MP4 技术报告")
        return self._validate_mp4_delivery_result(report, request), request, False

    def _run_picture_branch(self) -> dict[str, Any]:
        self.events.emit(
            "stage_started",
            stage="picture_workflow",
            detail="A+C 已通过正式结构检查；与外部翻译并行开始整片画面处理。",
        )
        try:
            raw_result = self.picture_runner(
                self._picture_config(),
                confirm_gpu_ready=True,
                confirm_gate_viewed=(
                    self.request.gates.picture_and_mux_30m_human_accepted
                ),
                mirror_output=False,
                cancelled=self._stopped,
            )
            result = self._validate_picture_result(raw_result)
            for warning in result.get("informational_warnings", []):
                message = (
                    warning.get("message", str(warning))
                    if isinstance(warning, Mapping)
                    else str(warning)
                )
                self.events.emit(
                    "warning",
                    stage="picture_workflow",
                    code=(
                        warning.get("code", "PICTURE_INFORMATIONAL_WARNING")
                        if isinstance(warning, Mapping)
                        else "PICTURE_INFORMATIONAL_WARNING"
                    ),
                    message=message,
                )
        except BaseException as exc:
            self.events.emit(
                "stage_failed",
                stage="picture_workflow",
                detail=f"画面分支失败并保留检查点：{exc}",
            )
            raise
        self.events.emit(
            "stage_completed",
            stage="picture_workflow",
            output=str(self.paths.processed_video),
            detail="画面分支已完成；等待翻译分支后再执行最终汇合。",
        )
        return result

    def run(self) -> dict[str, Any]:
        os.environ.pop("SUBTITLE_API_KEY", None)
        manifest = self._load_or_create_manifest()
        (self._translation_result_path().parent / "cancel.requested").unlink(missing_ok=True)
        (self.paths.workflow_root / "cancel.requested").unlink(missing_ok=True)
        self.events.emit(
            "job_started",
            workflow_mode="translation_picture_final",
            input_video=str(self.request.translation.input_video),
        )
        try:
            self._check_cancelled()
            picture_lock = threading.Lock()
            picture_thread: threading.Thread | None = None
            picture_result: dict[str, Any] | None = None
            picture_error: BaseException | None = None

            def picture_target() -> None:
                nonlocal picture_result, picture_error
                try:
                    picture_result = self._run_picture_branch()
                except BaseException as exc:
                    picture_error = exc

            def start_picture_after_asr() -> None:
                nonlocal picture_thread
                self._check_cancelled()
                with picture_lock:
                    if picture_thread is not None:
                        return
                    picture_thread = threading.Thread(
                        target=picture_target,
                        name="full-workflow-picture",
                    )
                    picture_thread.start()

            translation_result: dict[str, Any] | None = None
            translation_reused = False
            translation_error: BaseException | None = None
            try:
                translation_result, translation_reused = self._run_or_reuse_translation(
                    on_asr_ready=start_picture_after_asr,
                )
            except BaseException as exc:
                translation_error = exc
                self.events.emit(
                    "stage_failed",
                    stage="translation_flow",
                    detail=f"翻译分支失败并保留检查点：{exc}",
                )
            finally:
                self.api_key = ""

            # A complete reused translation bypasses its runner and therefore
            # has no ASR-ready callback. It is already a stronger handoff, so
            # start the picture branch here if it has not started yet.
            if translation_error is None and picture_thread is None:
                start_picture_after_asr()
            if picture_thread is not None:
                picture_thread.join()

            self._check_cancelled()

            if translation_error is not None and picture_error is not None:
                raise RuntimeError(
                    "A+C 后两个并行分支均失败："
                    f"翻译={translation_error}；画面={picture_error}"
                ) from translation_error
            if translation_error is not None:
                raise translation_error
            if picture_error is not None:
                raise picture_error
            if translation_result is None or picture_result is None:
                raise RuntimeError("并行分支结束但缺少正式结果")

            self.events.emit(
                "stage_completed",
                stage="parallel_branches",
                overall_percent=92,
                detail="翻译与画面两条分支均已通过技术检查。",
            )

            final_config = self._final_config(translation_result, picture_result)
            self.events.emit(
                "stage_started",
                stage="final_mux",
                overall_percent=93,
                detail="按画面报告的真实源起点和接缝执行最终薄汇合。",
            )
            final_report, final_reused = self._run_or_reuse_final_mux(final_config)
            final_technical = final_report.get("technical_validation", {})
            if isinstance(final_technical, Mapping):
                for warning in final_technical.get("warnings", []):
                    self.events.emit(
                        "warning",
                        stage="final_mux",
                        code="CONTAINER_DURATION_WARNING",
                        message=str(warning),
                    )
            self.events.emit(
                "stage_completed",
                stage="final_mux",
                overall_percent=98,
                output=str(self.paths.final_output),
            )

            mp4_report: dict[str, Any] | None = None
            mp4_request: MediaRepackageRequest | None = None
            mp4_reused = False
            mp4_error: str | None = None
            self._check_cancelled()
            if self.request.delivery.additional_mp4:
                self.events.emit(
                    "stage_started",
                    stage="mp4_delivery",
                    overall_percent=98,
                    detail="正式 MKV 已通过；正在追加 MP4 可切换软字幕交付。",
                )
                try:
                    mp4_report, mp4_request, mp4_reused = (
                        self._run_or_reuse_mp4_delivery()
                    )
                except Exception as exc:
                    mp4_error = str(exc)
                    self.events.emit(
                        "stage_failed",
                        stage="mp4_delivery",
                        overall_percent=99,
                        detail=f"追加 MP4 失败；正式 MKV 不受影响：{exc}",
                    )
                    self.events.emit(
                        "warning",
                        stage="mp4_delivery",
                        warning=f"正式 MKV 已完成，但追加 MP4 未交付：{exc}",
                    )
                else:
                    mp4_technical = mp4_report.get("technical_validation", {})
                    if isinstance(mp4_technical, Mapping):
                        for warning in mp4_technical.get("warnings", []):
                            self.events.emit(
                                "warning",
                                stage="mp4_delivery",
                                code="CONTAINER_DURATION_WARNING",
                                message=str(warning),
                            )
                    self.events.emit(
                        "stage_completed",
                        stage="mp4_delivery",
                        overall_percent=99,
                        output=str(mp4_request.output_path),
                        reused=mp4_reused,
                    )

            workflow_outputs = {
                "chinese_srt": translation_result["outputs"]["chinese_srt"],
                "translation_original_mkv": translation_result["outputs"]["final_mkv"],
                "processed_video": str(self.paths.processed_video),
                "final_mkv": str(self.paths.final_output),
                "technical_report": str(final_config.report_path),
            }
            subtitle_review = translation_result["outputs"].get("subtitle_review_md")
            if subtitle_review:
                workflow_outputs["subtitle_review_md"] = subtitle_review
            if mp4_report is not None and mp4_request is not None:
                workflow_outputs["final_mp4"] = str(mp4_request.output_path)
                workflow_outputs["mp4_technical_report"] = str(
                    mp4_request.technical_report_path
                )
            mp4_stage: dict[str, Any] = {
                "requested": self.request.delivery.additional_mp4,
                "status": (
                    "not_requested"
                    if not self.request.delivery.additional_mp4
                    else (
                        "technical_checks_passed"
                        if mp4_report is not None
                        else "failed"
                    )
                ),
                "reused": mp4_reused,
            }
            if mp4_request is not None:
                mp4_stage["output"] = str(mp4_request.output_path)
                mp4_stage["report"] = str(mp4_request.technical_report_path)
            if mp4_error is not None:
                mp4_stage["error"] = mp4_error
            workflow_warnings: list[str] = []
            asr = translation_result.get("asr", {})
            if isinstance(asr, Mapping):
                structure_warnings = asr.get("structure_warnings", {})
                if isinstance(structure_warnings, Mapping):
                    for stage, stage_warnings in structure_warnings.items():
                        if isinstance(stage_warnings, list):
                            workflow_warnings.extend(
                                f"ASR {stage}: {warning}"
                                for warning in stage_warnings
                            )
            for warning in picture_result.get("informational_warnings", []):
                workflow_warnings.append(
                    str(warning.get("message", warning))
                    if isinstance(warning, Mapping)
                    else str(warning)
                )
            final_technical = final_report.get("technical_validation", {})
            if isinstance(final_technical, Mapping):
                workflow_warnings.extend(
                    str(warning) for warning in final_technical.get("warnings", [])
                )
            if mp4_report is not None:
                mp4_technical = mp4_report.get("technical_validation", {})
                if isinstance(mp4_technical, Mapping):
                    workflow_warnings.extend(
                        str(warning)
                        for warning in mp4_technical.get("warnings", [])
                    )
            result = {
                "schema_version": 1,
                "status": "technical_checks_passed",
                "release_status": "candidate_for_human_review",
                "human_review_status": "pending",
                "human_acceptance": "pending",
                "warnings": workflow_warnings,
                "workflow_manifest": str(self.paths.manifest),
                "outputs": workflow_outputs,
                "technical_validation": {
                    "status": "passed",
                    "report_path": str(final_config.report_path),
                },
                "stages": {
                    "translation": {
                        "status": "technical_checks_passed",
                        "reused": translation_reused,
                        "report": str(self._translation_result_path()),
                    },
                    "picture": {
                        "status": "technical_checks_passed",
                        "reused": bool(picture_result.get("reused", False)),
                        "report": picture_result.get("report"),
                    },
                    "final_mux": {
                        "status": "technical_checks_passed",
                        "reused": final_reused,
                        "report": str(final_config.report_path),
                    },
                    "mp4_delivery": mp4_stage,
                },
            }
            _write_json(self.paths.result, result)
            _write_json(self.paths.manifest, {**manifest, "status": "technical_checks_passed"})
            self.events.emit("job_completed", **result)
            return result
        except BaseException as exc:
            self.api_key = ""
            _write_json(
                self.paths.manifest,
                {**manifest, "status": "cancelled" if isinstance(exc, TranslationCancelled) else "failed", "error": str(exc)},
            )
            raise
