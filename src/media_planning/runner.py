from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from translation_flow.events import EventEmitter
from translation_flow.runtime import DEFAULT_TOOL_ROOT, tool_root as resolve_tool_root
from video_pipeline.toolchain import Toolchain

from .contracts import MediaRepackageRequest, WORKFLOW_MODE
from .output import (
    OutputPlan,
    OutputPlanStatus,
    OutputTarget,
    StreamAction,
    plan_soft_subtitle_output,
    select_single_chinese_subtitle,
)
from .source import FFprobeSourceProbe, SourceFacts, parse_ffprobe_payload


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
SOFT_DURATION_WARNING_SECONDS = 0.75
HARD_DURATION_FAILURE_SECONDS = 2.0


class MediaRepackageError(RuntimeError):
    pass


def _streams(probe: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == kind
    ]


def _duration(probe: Mapping[str, Any]) -> float:
    value = probe.get("format", {}).get("duration")
    if value not in (None, "N/A"):
        duration = float(value)
        if duration > 0:
            return duration
    for stream in probe.get("streams", []):
        value = stream.get("duration")
        if value not in (None, "N/A"):
            duration = float(value)
            if duration > 0:
                return duration
    raise MediaRepackageError("FFprobe 没有给出可用的正时长，无法做输出校验")


def _stream_by_index(
    probe: Mapping[str, Any], index: int
) -> Mapping[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("index") == index:
            return stream
    raise MediaRepackageError(f"FFprobe 结果中缺少 stream index {index}")


def _publish_exclusive(candidate: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有结果：{destination}")
    os.rename(candidate, destination)


class MediaRepackageRunner:
    """Repackage one embedded Chinese subtitle without video/audio transcoding."""

    def __init__(
        self,
        request: MediaRepackageRequest,
        *,
        tool_root: str | Path | None = None,
        ffmpeg: str | Path | None = None,
        ffprobe: str | Path | None = None,
        process_runner: ProcessRunner = subprocess.run,
        emitter: EventEmitter | None = None,
        cancelled=None,
    ) -> None:
        self.request = request
        configured_root = resolve_tool_root(tool_root)
        toolchain = Toolchain.from_root(configured_root)
        self.ffmpeg = Path(ffmpeg or toolchain.ffmpeg)
        self.ffprobe = Path(ffprobe or toolchain.ffprobe)
        self.process_runner = process_runner
        self.cancelled = cancelled or (lambda: False)
        self.events = emitter or EventEmitter(
            request.job_id, request.job_root / "events.jsonl"
        )

    def _check_cancelled(self):
        from translation_flow.cancellation import check_cancelled
        check_cancelled(lambda: self.cancelled() or (self.request.job_root / "cancel.requested").exists())

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self.process_runner(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _run(self, args: list[str], label: str) -> subprocess.CompletedProcess[str]:
        result = self._invoke(args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1600:]
            raise MediaRepackageError(
                f"{label} 失败（退出码 {result.returncode}）：{detail}"
            )
        return result

    def _probe_payload(self, path: Path) -> Mapping[str, Any]:
        return FFprobeSourceProbe(
            self.ffprobe, process_runner=self.process_runner
        ).probe_payload(path)

    @staticmethod
    def _select_video(source: SourceFacts):
        videos = source.video_candidates
        if not videos:
            raise MediaRepackageError("FFprobe 没有找到可用视频轨")
        if len(videos) > 1:
            details = ", ".join(
                f"index={item.index}/codec={item.codec_name or 'unknown'}"
                for item in videos
            )
            raise MediaRepackageError(
                f"找到多条可用视频轨（{details}）；当前入口不会静默选择"
            )
        return videos[0]

    @staticmethod
    def _reject_non_direct_plan(plan: OutputPlan) -> None:
        if plan.status is OutputPlanStatus.DIRECT_MUX:
            return
        reason = ", ".join(plan.reasons) or plan.status.value
        if plan.target is OutputTarget.MP4:
            raise MediaRepackageError(
                "当前视频、音频或字幕不能按本任务规则直接封装为 MP4；"
                "需要转码或改用 MKV。本任务不会自动转码。"
                f"详情：{reason}"
            )
        raise MediaRepackageError(
            "当前视频、音频或字幕不能按本任务规则直接封装为 MKV；"
            "本任务不实现视频/音频转码或字幕 OCR。"
            f"详情：{reason}"
        )

    def _mux_command(
        self,
        *,
        plan: OutputPlan,
        candidate: Path,
        duration_limit: float | None = None,
    ) -> list[str]:
        subtitle_codec = plan.subtitle_codec
        if plan.subtitle.action is StreamAction.COPY:
            subtitle_codec = "copy"
        elif subtitle_codec == "subrip":
            subtitle_codec = "srt"
        args = [
            str(self.ffmpeg),
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-n",
            "-i",
            str(self.request.source_media),
            "-map",
            f"0:{plan.video.index}",
        ]
        for audio in plan.audio:
            args.extend(["-map", f"0:{audio.index}"])
        args.extend(["-map", f"0:{plan.subtitle.index}"])
        if duration_limit is not None:
            args.extend(["-t", f"{duration_limit:.3f}"])
        args.extend(
            [
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-c:s",
                subtitle_codec,
                "-disposition:v:0",
                "default",
                "-metadata:s:s:0",
                "language=zho",
                "-metadata:s:s:0",
                "title=Simplified Chinese",
                "-disposition:s:0",
                "default",
                str(candidate),
            ]
        )
        return args

    def _prove_ffmpeg_compatibility(self, plan: OutputPlan) -> None:
        suffix = ".mkv" if plan.target is OutputTarget.MKV else ".mp4"
        candidate = self.request.job_root / (
            f".direct-mux-preflight-{uuid.uuid4().hex}{suffix}"
        )
        command = self._mux_command(
            plan=plan,
            candidate=candidate,
            duration_limit=1.0,
        )
        try:
            result = self._invoke(command)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[-1600:]
                if plan.target is OutputTarget.MP4:
                    raise MediaRepackageError(
                        "当前 FFmpeg 无法在不转视频/音频的前提下封装为 MP4；"
                        "需要转码或改用 MKV。正式输出尚未开始。"
                        f"FFmpeg：{detail}"
                    )
                raise MediaRepackageError(
                    "当前 FFmpeg 无法在不转视频/音频的前提下封装为 MKV；"
                    "正式输出尚未开始。"
                    f"FFmpeg：{detail}"
                )
            if not candidate.is_file():
                raise MediaRepackageError(
                    "FFmpeg 兼容性预检返回成功，但没有产生短样候选"
                )
        finally:
            candidate.unlink(missing_ok=True)

    def _short_decode(self, candidate: Path, duration: float, position: float) -> None:
        length = min(1.0, duration)
        self._run(
            [
                str(self.ffmpeg),
                "-v",
                "error",
                "-xerror",
                "-nostdin",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(candidate),
                "-t",
                f"{length:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-sn",
                "-f",
                "null",
                "NUL" if os.name == "nt" else "/dev/null",
            ],
            f"输出抽点解码 {position:.3f}s",
        )

    @staticmethod
    def _validate_output(
        *,
        source_probe: Mapping[str, Any],
        output_probe: Mapping[str, Any],
        plan: OutputPlan,
    ) -> dict[str, Any]:
        source_video = _stream_by_index(source_probe, plan.video.index)
        source_audio = [
            _stream_by_index(source_probe, stream.index) for stream in plan.audio
        ]
        output_video = _streams(output_probe, "video")
        output_audio = _streams(output_probe, "audio")
        output_subtitles = _streams(output_probe, "subtitle")
        output_formats = {
            item.strip().casefold()
            for item in str(
                output_probe.get("format", {}).get("format_name", "")
            ).split(",")
            if item.strip()
        }
        expected_subtitle = "subrip" if plan.target is OutputTarget.MKV else "mov_text"
        container_ok = (
            "matroska" in output_formats
            if plan.target is OutputTarget.MKV
            else bool(output_formats.intersection({"mov", "mp4"}))
        )
        checks = {
            "target_container": container_ok,
            "one_video": len(output_video) == 1,
            "video_codec_copied": len(output_video) == 1
            and source_video.get("codec_name") == output_video[0].get("codec_name"),
            "video_dimensions_preserved": len(output_video) == 1
            and (source_video.get("width"), source_video.get("height"))
            == (output_video[0].get("width"), output_video[0].get("height")),
            "all_audio_streams_copied": len(source_audio) == len(output_audio),
            "audio_codecs_copied": [item.get("codec_name") for item in source_audio]
            == [item.get("codec_name") for item in output_audio],
            "one_chinese_subtitle": len(output_subtitles) == 1,
            "subtitle_codec": len(output_subtitles) == 1
            and output_subtitles[0].get("codec_name") == expected_subtitle,
            "subtitle_language_zho": len(output_subtitles) == 1
            and output_subtitles[0].get("tags", {}).get("language") == "zho",
            "subtitle_default": len(output_subtitles) == 1
            and output_subtitles[0].get("disposition", {}).get("default") == 1,
        }
        source_duration = _duration(source_probe)
        output_duration = _duration(output_probe)
        duration_difference = abs(source_duration - output_duration)
        checks["duration_preserved"] = (
            duration_difference <= HARD_DURATION_FAILURE_SECONDS
        )
        failures = [name for name, passed in checks.items() if not passed]
        if failures:
            raise MediaRepackageError(
                "媒体输出技术检查失败：" + ", ".join(failures)
            )
        warnings = []
        if duration_difference > SOFT_DURATION_WARNING_SECONDS:
            warnings.append(
                "容器报告时长与源媒体相差 "
                f"{duration_difference:.3f}s；按可播放的容器级偏差继续交付"
            )
        return {
            "status": "passed",
            "checks": checks,
            "source_duration_seconds": source_duration,
            "output_duration_seconds": output_duration,
            "duration_difference_seconds": duration_difference,
            "warnings": warnings,
            "source_audio_streams": len(source_audio),
            "output_audio_streams": len(output_audio),
            "output_subtitle_streams": len(output_subtitles),
        }

    def run(self) -> dict[str, Any]:
        self._check_cancelled()
        if not self.ffmpeg.is_file() or not self.ffprobe.is_file():
            raise FileNotFoundError(
                f"正式封装所需 FFmpeg/FFprobe 缺失：{self.ffmpeg}, {self.ffprobe}"
            )
        self.request.output_dir.mkdir(parents=True, exist_ok=True)
        self.request.job_root.mkdir(parents=True, exist_ok=True)
        output = self.request.output_path
        report_path = self.request.technical_report_path
        if output.resolve() == self.request.source_media.resolve():
            raise MediaRepackageError("输出不能覆盖源媒体")
        existing = [str(path) for path in (output, report_path) if path.exists()]
        if existing:
            raise FileExistsError("拒绝覆盖已有结果：" + ", ".join(existing))

        source_stat_before = self.request.source_media.stat()
        self.events.emit(
            "job_started",
            workflow_mode=WORKFLOW_MODE,
            input_media=str(self.request.source_media),
            output_target=self.request.output_target.value,
            external_services_used=False,
        )
        self.events.emit(
            "stage_started",
            stage="media_probe",
            overall_percent=5,
            message="正在读取 FFprobe 实际轨道信息。",
        )
        source_probe = self._probe_payload(self.request.source_media)
        source_facts = parse_ffprobe_payload(source_probe)
        video = self._select_video(source_facts)
        subtitle = select_single_chinese_subtitle(source_facts)
        plan = plan_soft_subtitle_output(
            source_facts,
            target=self.request.output_target,
            selected_video_index=video.index,
            selected_subtitle_index=subtitle.index,
        )
        self._reject_non_direct_plan(plan)
        self.events.emit(
            "stage_completed",
            stage="media_probe",
            overall_percent=15,
            message=(
                f"已确认视频轨 {video.index}、{len(plan.audio)} 条原音轨和"
                f"中文字幕轨 {subtitle.index}。"
            ),
        )
        self.events.emit(
            "stage_started",
            stage="compatibility_preflight",
            overall_percent=18,
            message="用当前 FFmpeg 做极短直接封装预检。",
        )
        self._check_cancelled()
        self._prove_ffmpeg_compatibility(plan)
        self.events.emit(
            "stage_completed",
            stage="compatibility_preflight",
            overall_percent=22,
            message="兼容性预检通过；视频和音频将 stream-copy。",
        )

        candidate = output.parent / (
            f".{output.name}.{uuid.uuid4().hex}.candidate{output.suffix}"
        )
        report_candidate = report_path.parent / (
            f".{report_path.name}.{uuid.uuid4().hex}.candidate.json"
        )
        mux_command = self._mux_command(plan=plan, candidate=candidate)
        output_published = False
        self.events.emit(
            "stage_started",
            stage="media_repackage",
            overall_percent=25,
            message=(
                f"正在生成 {self.request.output_target.value.upper()} 候选；"
                "视频/音频不转码。"
            ),
        )
        self._check_cancelled()
        # Once mux starts, finish its validation/publication as one safe unit.
        # Do not discard a completed mux merely because stop arrived mid-copy.
        try:
            self._run(mux_command, "媒体重新封装")
            if not candidate.is_file():
                raise MediaRepackageError(
                    "FFmpeg 返回成功，但没有产生媒体候选"
                )
            self.events.emit(
                "stage_completed",
                stage="media_repackage",
                overall_percent=88,
            )
            self.events.emit(
                "stage_started",
                stage="technical_validation",
                overall_percent=89,
                message="正在检查容器、轨道、时长和首/中/尾解码。",
            )
            output_probe = self._probe_payload(candidate)
            technical = self._validate_output(
                source_probe=source_probe,
                output_probe=output_probe,
                plan=plan,
            )
            for warning in technical.get("warnings", []):
                self.events.emit(
                    "warning",
                    stage="technical_validation",
                    code="CONTAINER_DURATION_WARNING",
                    message=str(warning),
                )
            duration = float(technical["output_duration_seconds"])
            positions = (
                0.0,
                max(0.0, duration / 2 - 0.5),
                max(0.0, duration - 1.0),
            )
            for position in positions:
                self._short_decode(candidate, duration, position)
            source_stat_after = self.request.source_media.stat()
            technical["checks"]["source_unchanged"] = (
                source_stat_before.st_size == source_stat_after.st_size
                and source_stat_before.st_mtime_ns == source_stat_after.st_mtime_ns
            )
            if not technical["checks"]["source_unchanged"]:
                raise MediaRepackageError("源媒体在任务期间发生了变化")
            technical.update(
                {
                    "short_decode_positions_seconds": [
                        round(item, 3) for item in positions
                    ],
                    "video_audio_stream_copy": True,
                    "subtitle_codec": plan.subtitle_codec,
                    "subtitle_language": "zho",
                    "ffmpeg_preflight": "passed",
                }
            )
            report = {
                "schema_version": 1,
                "status": "technical_checks_passed",
                "release_status": "candidate_for_human_review",
                "human_review_status": "pending",
                "human_acceptance": "pending",
                "workflow_mode": WORKFLOW_MODE,
                "source": {
                    "path": str(self.request.source_media),
                    "size_bytes": source_stat_before.st_size,
                    "mtime_ns": source_stat_before.st_mtime_ns,
                    "format_names": list(source_facts.format_names),
                    "video_stream_index": video.index,
                    "audio_stream_indices": [item.index for item in plan.audio],
                    "chinese_subtitle_stream_index": subtitle.index,
                },
                "output": {
                    "path": str(output),
                    "target": plan.target.value,
                    "subtitle_codec": plan.subtitle_codec,
                    "subtitle_language": "zho",
                    "subtitle_default": True,
                    "subtitle_mode": "soft_switchable",
                },
                "plan": plan.to_dict(),
                "mux_command": mux_command,
                "technical_validation": technical,
                "external_services_used": False,
            }
            report_candidate.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _publish_exclusive(candidate, output)
            output_published = True
            try:
                _publish_exclusive(report_candidate, report_path)
            except BaseException:
                output.unlink(missing_ok=True)
                output_published = False
                raise
        except BaseException:
            candidate.unlink(missing_ok=True)
            report_candidate.unlink(missing_ok=True)
            if output_published:
                output.unlink(missing_ok=True)
            raise

        self.events.emit(
            "stage_completed",
            stage="technical_validation",
            overall_percent=99,
            output=str(output),
            technical_validation="passed",
        )
        self.events.emit("artifact_ready", kind="final_media", path=str(output))
        result = {
            "outputs": {
                "final_media": str(output),
                "technical_report": str(report_path),
            },
            "technical_validation": {
                "status": "passed",
                "report_path": str(report_path),
            },
            "human_acceptance": "pending",
        }
        self.events.emit("job_completed", **result)
        return result
