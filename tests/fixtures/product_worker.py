"""Acceptance fixture: formal entry points + real FFmpeg, simulated ASR/models.

Never use this fixture on personal media. It is intentionally separate from the
desktop entry point and never demonstrates translation or demosaic quality.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_codex_integration import FakeAsr, FakeCodex
from translation_flow.runner import TranslationFlowRunner
from translation_flow.task_queue import request_info
from translation_flow.queue_cli import run_request
from full_workflow.runner import FullWorkflowRunner
from full_workflow.picture import PictureRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--tool-root', type=Path, required=True)
    parser.add_argument('--stop-after-work', action='store_true')
    parser.add_argument('--fail-translation', action='store_true')
    args = parser.parse_args()
    job = args.request.parent
    audit = job.parent.parent / 'fixture-model-calls.jsonl'
    ffmpeg = args.tool_root / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe'
    ffprobe = ffmpeg.with_name('ffprobe.exe')

    def record(kind):
        with audit.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'simulated': kind}) + '\n')

    class Asr(FakeAsr):
        def run(self, source, root, emit, *, cancelled=None):
            record('asr')
            return super().run(source, root, emit)

    class Model(FakeCodex):
        def chat(self, *a, **kw):
            if args.fail_translation:
                from translation_flow.translation.codex_provider import CodexQuotaExceeded
                raise CodexQuotaExceeded('simulated quota interruption')
            result = super().chat(*a, **kw)
            record('translation')
            if args.stop_after_work and len(self.calls) == 2:
                (job / 'cancel.requested').write_text('stop', encoding='utf-8')
            return result

    def picture(config, **kwargs):
        report_path = config.work_dir / 'reports/workflow-report.json'
        if report_path.is_file():
            return {**json.loads(report_path.read_text(encoding='utf-8')), 'reused': True}
        record('picture')
        config.output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-n', '-i', str(config.source),
                        '-map', '0:v:0', '-c:v', 'copy', '-an', '-sn', str(config.output)], check=True)
        probe = json.loads(subprocess.check_output([str(ffprobe), '-v', 'error', '-show_format', '-of', 'json', str(config.output)]))
        duration = float(probe['format']['duration'])
        stat = config.source.stat()
        report = dict(status='technical_checks_passed', scope='full', output=str(config.output),
                      source_identity=dict(path=str(config.source), size=stat.st_size, mtime_ns=stat.st_mtime_ns),
                      source_origin_seconds=0, output_seam_positions_seconds=[],
                      segment_mappings=[dict(index=0, source_start_seconds=0, source_end_seconds=duration,
                                             output_start_seconds=0, output_end_seconds=duration)],
                      final_duration_seconds=duration, route={'fixture': 'video copy, no demosaic'},
                      report=str(report_path), fixture_only=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding='utf-8')
        if kwargs.get('on_progress'):
            kwargs['on_progress'](1, 1)
        if args.stop_after_work:
            (job / 'cancel.requested').write_text('stop', encoding='utf-8')
        return report

    def translation(*a, **kw):
        return TranslationFlowRunner(*a, **kw, asr_factory=Asr, codex_provider_factory=Model)

    with patch('translation_flow.cli.TranslationFlowRunner', side_effect=translation), \
         patch('full_workflow.cli.FullWorkflowRunner', side_effect=lambda *a, **kw: FullWorkflowRunner(
             *a, **kw, translation_runner_factory=translation, picture_runner=picture)), \
         patch('full_workflow.picture_cli.PictureRunner', side_effect=lambda *a, **kw: PictureRunner(
             *a, **kw, picture_runner=picture)), \
         patch('translation_flow.translation.cli.CodexProvider', Model):
        return run_request(args.request, request_info(args.request)['request_digest'], str(args.tool_root))


if __name__ == '__main__':
    raise SystemExit(main())
