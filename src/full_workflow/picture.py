"""Deliver picture-only jobs through Jasna and the existing final mux."""
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

from final_mux import FinalMuxConfig, FinalMuxWorkflow
from jasna_picture import PictureWorkflowConfig, run_picture_workflow
from translation_flow.cancellation import check_cancelled
from translation_flow.contracts import _JOB_ID
from translation_flow.events import EventEmitter
from translation_flow.runtime import tool_root, jasna_path, acceptance_report
from translation_flow.runner import _safe_stem
from translation_flow.subtitle_detection import sidecars
from .contracts import FullWorkflowGates
from .runner import _load_json, _write_json, _same_decimal, validate_picture_result


@dataclass(frozen=True)
class PictureRequest:
    job_id: str
    input_video: Path
    output_dir: Path
    output_target: str
    gates: FullWorkflowGates

    @classmethod
    def from_mapping(cls, value):
        allowed = {'schema_version', 'workflow_mode', 'job_id', 'input_video', 'output_dir', 'output_target', 'gates'}
        if not isinstance(value, Mapping) or set(value) - allowed:
            raise ValueError('去马请求只接受本地公开配置')
        if value.get('schema_version') != 1 or value.get('workflow_mode') != 'picture_only':
            raise ValueError('不支持的去马请求')
        job_id = value.get('job_id', '')
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id) or job_id in {'.', '..'}:
            raise ValueError('无效的任务名称')
        for name in ('input_video', 'output_dir'):
            if not isinstance(value.get(name), str) or not value[name].strip():
                raise ValueError('请选择输入视频和保存位置')
        source = Path(value['input_video']).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() == '.srt':
            raise ValueError('SRT 只有字幕，去马需要视频文件。')
        target = value.get('output_target')
        if target not in {'mkv', 'mp4'}:
            raise ValueError('请选择 MKV 或 MP4')
        gate = value.get('gates', {})
        if not isinstance(gate, dict) or set(gate) - {'picture_and_mux_30m_human_accepted'}:
            raise ValueError('无效的路线验收配置')
        return cls(job_id, source, Path(value['output_dir']).resolve(), target,
                   FullWorkflowGates.from_mapping(gate))


def preserve_sidecars(source: Path, output: Path) -> list[str]:
    """Keep imported external subtitles discoverable beside the renamed video."""
    outputs = []
    for subtitle in sidecars(source):
        target = output.with_name(output.stem + subtitle.name[len(source.stem):])
        content = subtitle.read_bytes()
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError('目标外挂字幕已存在且内容不同，保留原文件。')
        else:
            with target.open('xb') as handle:
                handle.write(content)
        outputs.append(str(target))
    return outputs


class PictureRunner:
    def __init__(self, request, *, tools=None, jasna=None, gate=None, emitter=None,
                 cancelled=None, picture_runner=run_picture_workflow, mux_factory=FinalMuxWorkflow):
        self.request = request
        self.root = request.output_dir / '.picture-workflow' / request.job_id
        self.events = emitter or EventEmitter(request.job_id, self.root / 'events.jsonl')
        self.cancelled = cancelled or (lambda: False)
        self.picture_runner, self.mux_factory = picture_runner, mux_factory
        tools = tool_root(tools)
        self.ffmpeg = tools / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
        self.ffprobe = self.ffmpeg.with_name('ffprobe.exe')
        self.picture_config = PictureWorkflowConfig(
            scope='full', source=request.input_video, work_dir=self.root / 'picture',
            output=self.root / 'processed.video-only.mkv', jasna=jasna_path(jasna, tools=tools),
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe, gate_report=acceptance_report(gate, tools=tools), start=None)
        self.output = request.output_dir / f'{_safe_stem(request.input_video.stem)}.{request.job_id}.restored.{request.output_target}'

    def stopped(self):
        return self.cancelled() or (self.root / 'cancel.requested').exists()

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'cancel.requested').unlink(missing_ok=True)
        stat = self.request.input_video.stat()
        identity = dict(path=str(self.request.input_video), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        manifest = dict(source_identity=identity, output=str(self.output),
                        jasna=str(self.picture_config.jasna), gate=str(self.picture_config.gate_report))
        manifest_path = self.root / 'workflow.json'
        if manifest_path.exists() and _load_json(manifest_path, '去马任务') != manifest:
            raise ValueError('去马任务的源片或设置已改变，请建立新任务。')
        _write_json(manifest_path, manifest)
        self.events.emit('job_started', workflow_mode='picture_only', input_video=str(self.request.input_video))
        check_cancelled(self.stopped)
        self.events.emit('stage_started', stage='picture_workflow', overall_percent=1, detail='正在去马。')
        picture = self.picture_runner(self.picture_config, confirm_gpu_ready=True,
                                      confirm_gate_viewed=self.request.gates.picture_and_mux_30m_human_accepted,
                                      mirror_output=False, cancelled=self.stopped,
                                      on_progress=lambda done, total: self.events.emit(
                                          'stage_progress', stage='picture_workflow', overall_percent=round(85*done/total, 1),
                                          detail=f'去马已完成 {done}/{total} 段。'))
        picture = validate_picture_result(picture, self.request.input_video, self.picture_config.output)
        self.events.emit('stage_completed', stage='picture_workflow', overall_percent=86, reused=bool(picture.get('reused')))
        check_cancelled(self.stopped)
        config = FinalMuxConfig(self.picture_config.output, self.request.input_video, None,
                                str(picture['source_origin_seconds']), self.output, self.root / 'final-mux',
                                tuple(str(s) for s in picture['output_seam_positions_seconds']),
                                preserve_source_subtitles=True)
        self.events.emit('stage_started', stage='final_mux', overall_percent=90, detail='正在封装原声音和已有字幕。')
        reused = config.output.is_file() and config.report_path.is_file()
        if reused:
            report = _load_json(config.report_path, '去马封装报告')
        else:
            report = self.mux_factory(self.ffmpeg, self.ffprobe).run(config)
        lineage = report.get('input_lineage', {})
        checks = report.get('technical_validation', {}).get('checks', {})
        if (report.get('status') != 'technical_checks_passed' or not config.output.is_file()
            or report.get('output') != str(config.output) or report.get('subtitle_mode') != 'source'
            or lineage.get('source_media') != str(config.source_media)
            or lineage.get('processed_video') != str(config.processed_video)
            or not _same_decimal(report.get('source_start_seconds'), config.source_start_seconds)
            or report.get('seam_positions_seconds') != [float(s) for s in config.seam_positions_seconds]
            or not checks or not all(value is True for value in checks.values())):
            raise ValueError('去马成品或封装报告不完整，保留检查点。')
        self.events.emit('stage_completed', stage='final_mux', overall_percent=100, reused=reused)
        result = dict(status='technical_checks_passed', workflow_mode='picture_only',
                      outputs=dict(final_media=str(self.output), technical_report=str(config.report_path)),
                      technical_validation=dict(status='passed', report_path=str(config.report_path)),
                      release_status='candidate_for_human_review', human_review_status='pending')
        for index, path in enumerate(preserve_sidecars(self.request.input_video, self.output), 1):
            result['outputs'][f'source_subtitle_{index}'] = path
        _write_json(self.root / 'job.result.json', result)
        self.events.emit('job_completed', overall_percent=100, **result)
        return result
