"""Local SR delivery and the translation/SR join. Existing modes are unchanged."""
from dataclasses import dataclass
from dataclasses import replace
import math
from pathlib import Path

from final_mux import FinalMuxConfig, FinalMuxWorkflow
from full_workflow.runner import FullWorkflowRunner, FullWorkflowPaths
from translation_flow.contracts import _JOB_ID
from translation_flow.events import EventEmitter
from translation_flow.runtime import tool_root
from translation_flow.runner import _safe_stem
from translation_flow.subtitle_detection import sidecars
from .workflow import SRConfig, read_json, write_json, identity, probe, run_command, run_super_resolution
from .runtime import MODEL, model_spec, runtime_root


def sr_options(value, *, standalone=False):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"max_height", "model"}:
        raise ValueError("超分只需要选择目标高度和模型。")
    height = value.get("max_height", 1080)
    if type(height) is not int or height not in {720, 1080}:
        raise ValueError("轻量超分目标请选择 720p 或 1080p。")
    model = value.get("model", MODEL)
    model_spec(model)
    if model != MODEL and not standalone:
        raise ValueError("NomosUni OTF Medium 当前仅用于单独超分。")
    # Omit the legacy default, so existing Lite job manifests remain valid.
    return {"max_height": height, **({"model": model} if model != MODEL else {})}


@dataclass(frozen=True)
class SRRequest:
    job_id: str
    input_video: Path
    output_dir: Path
    output_target: str
    options: dict
    preview_start: float | None = None

    @classmethod
    def from_mapping(cls, value):
        allowed = {"schema_version", "workflow_mode", "job_id", "input_video", "output_dir",
                   "output_target", "super_resolution", "preview_start_seconds"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("超分请求只接受本地公开配置。")
        if value.get("schema_version") != 1 or value.get("workflow_mode") != "super_resolution":
            raise ValueError("不支持的超分请求。")
        job = value.get("job_id", "")
        if not isinstance(job, str) or not _JOB_ID.fullmatch(job):
            raise ValueError("无效的任务名称。")
        if not value.get("input_video") or not value.get("output_dir"):
            raise ValueError("请选择视频和保存位置。")
        source = Path(value["input_video"]).resolve()
        if not source.is_file() or source.suffix.lower() == ".srt":
            raise ValueError("超分需要实际视频文件。")
        if value.get("output_target") not in {"mkv", "mp4"}:
            raise ValueError("请选择 MKV 或 MP4。")
        start = value.get("preview_start_seconds")
        if start is not None and (type(start) not in {int, float} or not math.isfinite(start) or start < 0):
            raise ValueError("短片起点必须是非负秒数。")
        return cls(job, source, Path(value["output_dir"]).resolve(), value["output_target"],
                   sr_options(value.get("super_resolution"), standalone=True), start)


def progress(events, frames):
    events.emit("stage_progress", stage="super_resolution", processed_frames=frames,
                detail=f"超分已处理 {frames} 帧；完成后拼接与封装。")


class SRRunner:
    def __init__(self, request, *, tools=None, emitter=None, cancelled=lambda: False,
                 sr_runner=run_super_resolution, mux_factory=FinalMuxWorkflow):
        self.request, self.cancelled = request, cancelled
        self.sr_runner, self.mux_factory = sr_runner, mux_factory
        self.root = request.output_dir / ".super-resolution" / request.job_id
        self.events = emitter or EventEmitter(request.job_id, self.root / "events.jsonl")
        self.tool_root = tool_root(tools)
        self.ffmpeg = self.tool_root / "ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"
        self.ffprobe = self.ffmpeg.with_name("ffprobe.exe")
        suffix = "preview" if request.preview_start is not None else "upscaled"
        self.output = request.output_dir / f"{_safe_stem(request.input_video.stem)}.{request.job_id}.{suffix}.{request.output_target}"

    def stopped(self):
        return self.cancelled() or (self.root / "cancel.requested").exists()

    def render_preview(self, config, report, mux):
        """Sample audio is decoded and trimmed exactly; never fast-seek/copied.

        Preview comparisons deliberately show the image without subtitles. Full
        film delivery below continues to preserve original audio/subtitle tracks.
        """
        start, duration = report['source_origin_seconds'], report['final_duration_seconds']
        pending = self.root / ('preview.pending.' + self.request.output_target)
        run_command([self.ffmpeg, '-v', 'error', '-nostdin', '-y', '-i', config.output,
                     '-i', self.request.input_video, '-map', '0:v:0', '-map', '1:a?',
                     '-c:v', 'copy', '-af', f'atrim=start={start}:duration={duration},asetpts=PTS-{start}/TB',
                     '-c:a', 'aac', '-t', str(duration), '-avoid_negative_ts', 'disabled', pending], self.stopped)
        actual = probe(self.ffprobe, pending, self.stopped)
        source = probe(self.ffprobe, self.request.input_video, self.stopped)
        videos = [s for s in actual['streams'] if s['codec_type'] == 'video']
        checks = dict(video_present=len(videos) == 1,
                      audio_tracks_preserved=sum(s['codec_type'] == 'audio' for s in actual['streams']) ==
                                             sum(s['codec_type'] == 'audio' for s in source['streams']),
                      duration_matches=abs(float(actual['format']['duration']) - duration) < 0.25,
                      video_starts_at_zero=bool(videos) and abs(float(videos[0].get('start_time', 0))) < 0.1)
        if not all(checks.values()):
            raise ValueError('短片预览的音视频轨道或时间窗口不匹配，保留超分检查点。')
        pending.rename(self.output)
        result = dict(status='technical_checks_passed', output=str(self.output),
                      input_lineage=dict(processed_video=str(config.output), source_media=str(self.request.input_video)),
                      source_start_seconds=str(start), subtitle_mode='none_for_image_comparison',
                      audio_policy='preview_aac_accurate_trim', technical_validation=dict(checks=checks))
        write_json(mux.report_path, result)
        return result

    def run(self):
        request = self.request
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cancel.requested").unlink(missing_ok=True)
        manifest = dict(source=identity(request.input_video), output=str(self.output),
                        options=request.options, preview_start=request.preview_start)
        path = self.root / "job.json"
        if path.exists() and read_json(path) != manifest:
            raise ValueError("任务设置已改变，请建立新任务。")
        write_json(path, manifest)
        selected_model = request.options.get("model", MODEL)
        model_label = model_spec(selected_model)["label"]
        self.events.emit("job_started", workflow_mode="super_resolution", sr_model=selected_model,
                         sr_model_label=model_label)
        config = SRConfig(request.input_video, self.root / "video", self.root / "video-only.mkv",
                          self.ffmpeg, self.ffprobe, runtime=runtime_root(self.tool_root), **request.options,
                          preview_start=request.preview_start or 0,
                          preview_duration=15 if request.preview_start is not None else None)
        self.events.emit("stage_started", stage="super_resolution", detail=f"正在本机使用 {model_label} 单独超分。")
        report = self.sr_runner(config, cancelled=self.stopped,
                                on_progress=lambda frames: progress(self.events, frames))
        self.events.emit("stage_completed", stage="super_resolution", reused=report.get("reused", False))
        mux = FinalMuxConfig(config.output, request.input_video, None,
                             str(report["source_origin_seconds"]), self.output, self.root / "final-mux",
                             (), preserve_source_subtitles=True)
        self.events.emit("stage_started", stage="final_mux", detail="正在封装原声音和已有字幕。")
        if mux.report_path.exists() and self.output.exists():
            final = read_json(mux.report_path)
        elif request.preview_start is not None:
            final = self.render_preview(config, report, mux)
        else:
            final = self.mux_factory(self.ffmpeg, self.ffprobe).run(mux)
        lineage = final.get("input_lineage", {})
        checks = final.get("technical_validation", {}).get("checks", {})
        if (final.get("status") != "technical_checks_passed" or final.get("output") != str(self.output)
                or lineage.get("processed_video") != str(config.output)
                or lineage.get("source_media") != str(request.input_video)
                or not self.output.is_file() or not self.output.stat().st_size
                or not checks or not all(v is True for v in checks.values())):
            raise ValueError("超分封装报告或产物不完整，保留检查点。")
        outputs = dict(final_media=str(self.output), technical_report=str(mux.report_path),
                       super_resolution_report=report["report"])
        if request.preview_start is None:
            # Copy complete external subtitles only for a complete film.
            for index, subtitle in enumerate(sidecars(request.input_video)):
                target = self.output.with_name(self.output.stem + subtitle.name[len(request.input_video.stem):])
                data = subtitle.read_bytes()
                if target.exists():
                    if target.read_bytes() != data:
                        raise ValueError("已有外挂字幕不同，不会覆盖。")
                else:
                    with target.open("xb") as handle:
                        handle.write(data)
                outputs[f"source_subtitle_{index}"] = str(target)
        else:
            comparison = self.output.with_name(self.output.stem + ".compare.mp4")
            if not comparison.exists():
                # Same interval, frame rate and displayed height; left original,
                # right SR. Original audio comes from the completed preview.
                filters = (f"[0:v]scale=trunc(iw*sar/2)*2:ih,setsar=1,fps={report['route']['fps']}:start_time=0,"
                           f"trim=start_frame={report['route']['first_frame']}:end_frame={report['route']['end_frame']},"
                           "setpts=PTS-STARTPTS,scale=-2:540,setsar=1[left];"
                           "[1:v]scale=-2:540,setsar=1[right];[left][right]hstack=shortest=1[v]")
                pending = self.root / "comparison.pending.mp4"
                run_command([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", request.input_video,
                             "-i", self.output, "-filter_complex", filters, "-map", "[v]", "-map", "1:a?",
                             "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-t", str(report["final_duration_seconds"]), pending], self.stopped)
                pending.rename(comparison)
            outputs["comparison"] = str(comparison)
        result = dict(status="technical_checks_passed", workflow_mode="super_resolution", outputs=outputs,
                      super_resolution=dict(model=selected_model, model_label=model_label),
                      technical_validation=dict(status="passed", report_path=str(mux.report_path)),
                      release_status="candidate_for_human_review", human_review_status="pending")
        write_json(self.root / "job.result.json", result)
        self.events.emit("job_completed", overall_percent=100, **result)
        return result


class TranslationSRRunner(FullWorkflowRunner):
    """Reuse the existing ASR barrier, translation recovery, and final mux join."""
    def __init__(self, request, api_key, *, sr_runner=run_super_resolution, **kwargs):
        super().__init__(request, api_key, **kwargs)
        self.sr_runner = sr_runner
        self.paths = FullWorkflowPaths(self.paths.workflow_root,
                                       self.paths.workflow_root / "upscaled.video-only.mkv",
                                       self.paths.final_output.with_name(self.paths.final_output.name.replace(".restored.", ".upscaled.")))

    def _manifest_payload(self):
        value = super()._manifest_payload()
        value["workflow_mode"] = "translation_super_resolution"
        value["picture"] = dict(scope="full", output=str(self.paths.processed_video),
                                super_resolution=self.request.super_resolution)
        value["gates"].pop("picture_and_mux_30m_human_accepted", None)
        return value

    def _final_config(self, translation_result, picture_result):
        # SR chunks are one continuous frame stream, not independently trimmed
        # source scenes. Do not turn every 64-frame batch into another decode gate.
        return replace(super()._final_config(translation_result, picture_result), seam_positions_seconds=())

    def _run_picture_branch(self):
        self.events.emit("stage_started", stage="super_resolution", detail="识别已结束；超分与翻译并行。")
        try:
            config = SRConfig(self.request.translation.input_video, self.paths.workflow_root / "super-resolution",
                              self.paths.processed_video, self.ffmpeg, self.ffprobe,
                              runtime=runtime_root(self.tool_root), **self.request.super_resolution)
            result = self.sr_runner(config, cancelled=self._stopped,
                                    on_progress=lambda frames: progress(self.events, frames))
            result = self._validate_picture_result(result)
        except BaseException:
            self.events.emit("stage_failed", stage="super_resolution", detail="超分未完成，已保留完成段。")
            raise
        self.events.emit("stage_completed", stage="super_resolution", reused=result.get("reused", False))
        return result
