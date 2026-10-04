"""Safe stop boundaries using fake FFmpeg/FFprobe, never real media."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_planning.cli import main
from media_planning.runner import MediaRepackageRunner
from translation_flow.translation.codex_provider import TranslationCancelled
import test_media_repackage as fixture
from test_media_repackage import _completed, _probe, _stream, _Events


class QueueMediaStopTests(unittest.TestCase):
    def test_stop_before_mux_then_same_request_can_complete(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            helpers = fixture.MediaRepackageRunnerTests()
            request = helpers._request(root, "mkv")
            ffmpeg, ffprobe = helpers._tools(root)
            stopped = False
            muxes = []
            streams = _probe([_stream(0, "video", "h264", width=160, height=90),
                              _stream(1, "audio", "aac"),
                              _stream(2, "subtitle", "subrip", language="zho", default=True)])

            class Events(_Events):
                def emit(self, event, **data):
                    nonlocal stopped
                    super().emit(event, **data)
                    if event == "stage_completed" and data.get("stage") == "compatibility_preflight":
                        stopped = True

            def process(args, **kwargs):
                if args[0] == str(ffprobe):
                    return _completed(args, stdout=json.dumps(streams))
                if args[-1].casefold() not in {"nul", "/dev/null"}:
                    Path(args[-1]).write_bytes(b"offline candidate")
                    muxes.append(args)
                return _completed(args)

            with self.assertRaises(TranslationCancelled):
                MediaRepackageRunner(request, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                     emitter=Events(), process_runner=process, cancelled=lambda: stopped).run()
            self.assertFalse(request.output_path.exists())
            self.assertEqual(1, len(muxes))  # Short compatibility fixture only.
            result = MediaRepackageRunner(request, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                          emitter=_Events(), process_runner=process).run()
            self.assertTrue(request.output_path.exists())
            self.assertEqual("passed", result["technical_validation"]["status"])

    def test_stop_during_mux_allows_validation_and_publication_to_finish(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            helpers = fixture.MediaRepackageRunnerTests()
            request = helpers._request(root, "mkv")
            ffmpeg, ffprobe = helpers._tools(root)
            stopped = False
            streams = _probe([_stream(0, "video", "h264", width=160, height=90),
                              _stream(1, "audio", "aac"),
                              _stream(2, "subtitle", "subrip", language="zho", default=True)])

            def process(args, **kwargs):
                nonlocal stopped
                if args[0] == str(ffprobe):
                    return _completed(args, stdout=json.dumps(streams))
                if args[-1].casefold() not in {"nul", "/dev/null"}:
                    Path(args[-1]).write_bytes(b"offline candidate")
                    if ".candidate" in args[-1]:
                        stopped = True
                return _completed(args)

            result = MediaRepackageRunner(request, ffmpeg=ffmpeg, ffprobe=ffprobe, emitter=_Events(),
                                          process_runner=process, cancelled=lambda: stopped).run()
            self.assertTrue(stopped)
            self.assertTrue(request.output_path.exists())
            self.assertTrue(request.technical_report_path.exists())
            self.assertEqual("passed", result["technical_validation"]["status"])

    def test_cli_reports_cancellation_as_recoverable_exit_two(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "fixture.data"
            source.write_bytes(b"not real media")
            payload = {"schema_version": 1, "workflow_mode": "media_repackage", "job_id": "fixture",
                       "source_media": str(source), "output_dir": str(root), "output_target": "mkv"}
            path = root / "request.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("media_planning.cli.MediaRepackageRunner") as runner:
                runner.return_value.run.side_effect = TranslationCancelled("offline stop")
                self.assertEqual(2, main(["--request", str(path)]))
            events = (root / ".media-repackage/fixture/events.jsonl").read_text(encoding="utf-8")
            self.assertEqual("job_cancelled", json.loads(events.strip())["event"])
