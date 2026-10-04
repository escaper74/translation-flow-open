from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from media_planning.contracts import MediaRepackageRequest
from media_planning.runner import MediaRepackageError, MediaRepackageRunner


def _completed(args: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def _stream(
    index: int,
    codec_type: str,
    codec_name: str,
    *,
    default: bool = False,
    language: str = "",
    width: int | None = None,
    height: int | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec_name,
        "disposition": {"default": int(default), "attached_pic": 0},
        "tags": {"language": language},
    }
    if width is not None:
        item["width"] = width
    if height is not None:
        item["height"] = height
    return item


def _probe(
    streams: list[dict[str, object]],
    *,
    target: str = "mkv",
    duration: float = 4.0,
) -> dict:
    return {
        "format": {
            "format_name": (
                "matroska,webm"
                if target == "mkv"
                else "mov,mp4,m4a,3gp,3g2,mj2"
            ),
            "duration": f"{duration:.3f}",
        },
        "streams": streams,
    }


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **payload: object) -> None:
        self.items.append((event, payload))


class MediaRepackageContractTests(unittest.TestCase):
    def test_request_uses_a_separate_keyless_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "renamed.data"
            source.write_bytes(b"x")
            request = MediaRepackageRequest.from_mapping(
                {
                    "schema_version": 1,
                    "workflow_mode": "media_repackage",
                    "job_id": "media-123",
                    "source_media": str(source),
                    "output_dir": str(root / "out"),
                    "output_target": "mp4",
                }
            )
            self.assertEqual(source.resolve(), request.source_media)
            self.assertEqual("renamed.zho-softsub.mp4", request.output_path.name)
            self.assertNotEqual(source.resolve(), request.output_path)

            with self.assertRaisesRegex(ValueError, "provider"):
                MediaRepackageRequest.from_mapping(
                    {
                        "schema_version": 1,
                        "workflow_mode": "media_repackage",
                        "job_id": "media-123",
                        "source_media": str(source),
                        "output_dir": str(root / "out"),
                        "output_target": "mkv",
                        "provider": {"id": "deepseek"},
                    }
                )


class MediaRepackageRunnerTests(unittest.TestCase):
    def _request(self, root: Path, target: str) -> MediaRepackageRequest:
        source = root / "movie.unknown"
        source.write_bytes(b"source stays read-only")
        return MediaRepackageRequest.from_mapping(
            {
                "schema_version": 1,
                "workflow_mode": "media_repackage",
                "job_id": f"media-{target}",
                "source_media": str(source),
                "output_dir": str(root / "out"),
                "output_target": target,
            }
        )

    @staticmethod
    def _tools(root: Path) -> tuple[Path, Path]:
        ffmpeg = root / "ffmpeg.exe"
        ffprobe = root / "ffprobe.exe"
        ffmpeg.write_bytes(b"tool")
        ffprobe.write_bytes(b"tool")
        return ffmpeg, ffprobe

    def test_mp4_known_incompatibility_stops_before_any_ffmpeg_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request(root, "mp4")
            ffmpeg, ffprobe = self._tools(root)
            calls: list[list[str]] = []
            source_probe = _probe(
                [
                    _stream(0, "video", "ffv1", width=160, height=90),
                    _stream(1, "audio", "pcm_s24le"),
                    _stream(2, "subtitle", "subrip", language="zho", default=True),
                ]
            )

            def runner(args: list[str], **_: object):
                calls.append(list(args))
                if args[0] == str(ffmpeg):
                    self.fail("FFmpeg must not run after a blocked stream plan")
                return _completed(args, stdout=json.dumps(source_probe))

            with self.assertRaisesRegex(
                MediaRepackageError, "需要转码或改用 MKV"
            ):
                MediaRepackageRunner(
                    request,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    process_runner=runner,
                    emitter=_Events(),
                ).run()

            self.assertEqual(1, len(calls))
            self.assertEqual(str(ffprobe), calls[0][0])
            self.assertFalse(request.output_path.exists())
            self.assertFalse(request.technical_report_path.exists())

    def test_mp4_maps_one_video_all_audio_and_only_one_zho_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request(root, "mp4")
            ffmpeg, ffprobe = self._tools(root)
            events = _Events()
            calls: list[list[str]] = []
            source_probe = _probe(
                [
                    _stream(4, "video", "h264", default=True, width=160, height=90),
                    _stream(7, "audio", "aac"),
                    _stream(9, "audio", "ac3"),
                    _stream(12, "subtitle", "subrip", language="eng"),
                    _stream(15, "subtitle", "subrip", language="zho", default=True),
                ]
            )
            output_probe = _probe(
                [
                    _stream(0, "video", "h264", default=True, width=160, height=90),
                    _stream(1, "audio", "aac"),
                    _stream(2, "audio", "ac3"),
                    _stream(3, "subtitle", "mov_text", language="zho", default=True),
                ],
                target="mp4",
                duration=5.0,
            )

            def runner(args: list[str], **_: object):
                calls.append(list(args))
                if args[0] == str(ffprobe):
                    payload = source_probe if args[-1] == str(request.source_media) else output_probe
                    return _completed(args, stdout=json.dumps(payload))
                if args[-1].casefold() not in {"nul", "/dev/null"}:
                    Path(args[-1]).write_bytes(b"candidate")
                return _completed(args)

            before = request.source_media.read_bytes()
            result = MediaRepackageRunner(
                request,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                process_runner=runner,
                emitter=events,
            ).run()

            self.assertEqual(before, request.source_media.read_bytes())
            self.assertTrue(request.output_path.is_file())
            self.assertTrue(request.technical_report_path.is_file())
            self.assertEqual(str(request.output_path), result["outputs"]["final_media"])
            mux_calls = [
                args
                for args in calls
                if args[0] == str(ffmpeg)
                and args[-1].casefold() not in {"nul", "/dev/null"}
            ]
            self.assertEqual(2, len(mux_calls))
            full_mux = mux_calls[-1]
            mapped = [
                full_mux[index + 1]
                for index, value in enumerate(full_mux)
                if value == "-map"
            ]
            self.assertEqual(["0:4", "0:7", "0:9", "0:15"], mapped)
            self.assertNotIn("0:12", mapped)
            self.assertEqual("copy", full_mux[full_mux.index("-c:v") + 1])
            self.assertEqual("copy", full_mux[full_mux.index("-c:a") + 1])
            self.assertEqual("mov_text", full_mux[full_mux.index("-c:s") + 1])
            report = json.loads(
                request.technical_report_path.read_text(encoding="utf-8")
            )
            self.assertEqual("technical_checks_passed", report["status"])
            self.assertTrue(report["technical_validation"]["video_audio_stream_copy"])
            self.assertEqual(1, len(report["technical_validation"]["warnings"]))
            self.assertEqual("pending", report["human_acceptance"])
            self.assertIn(
                "warning",
                [event for event, _payload in events.items],
            )
            self.assertEqual("job_completed", events.items[-1][0])

    def test_multiple_video_streams_fail_before_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request(root, "mkv")
            ffmpeg, ffprobe = self._tools(root)
            source_probe = _probe(
                [
                    _stream(0, "video", "h264", default=True),
                    _stream(1, "video", "hevc"),
                    _stream(2, "audio", "aac"),
                    _stream(3, "subtitle", "subrip", language="zho"),
                ]
            )

            def runner(args: list[str], **_: object):
                if args[0] == str(ffmpeg):
                    self.fail("ambiguous video must fail before FFmpeg")
                return _completed(args, stdout=json.dumps(source_probe))

            with self.assertRaisesRegex(MediaRepackageError, "多条可用视频轨"):
                MediaRepackageRunner(
                    request,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    process_runner=runner,
                    emitter=_Events(),
                ).run()

    def test_existing_output_blocks_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request(root, "mkv")
            ffmpeg, ffprobe = self._tools(root)
            request.output_dir.mkdir(parents=True)
            request.output_path.write_bytes(b"existing")

            def runner(*_: object, **__: object):
                self.fail("existing output must block before probing or muxing")

            with self.assertRaises(FileExistsError):
                MediaRepackageRunner(
                    request,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    process_runner=runner,
                    emitter=_Events(),
                ).run()
            self.assertEqual(b"existing", request.output_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
