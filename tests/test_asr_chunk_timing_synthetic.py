"""Real FFmpeg audio timing; Whisper is replaced by a tone detector."""
from contextlib import redirect_stdout
import array
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from workflow.scripts import transcribe_whisper_chunked as chunks


class ChunkAudioTimingTests(unittest.TestCase):
    def test_delayed_audio_stays_on_media_timeline_across_chunks(self):
        tool_root = os.environ.get("ASR_TIMING_TOOL_ROOT")
        if not tool_root:
            self.skipTest("ASR_TIMING_TOOL_ROOT is not set")
        ffmpeg = Path(tool_root) / "ffmpeg.exe"
        ffprobe = ffmpeg.with_name("ffprobe.exe")
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest("FFmpeg and FFprobe are required")

        def execute(args):
            result = subprocess.run(args, capture_output=True, timeout=30)
            if result.returncode:
                self.fail(result.stderr.decode(errors="replace")[-2000:])

        for extension, video_codec, audio_codec in (
            ("wmv", "wmv2", "wmav2"),
            ("mp4", "mpeg4", "aac"),
        ):
            with self.subTest(container=extension), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / f"source.{extension}"
                # Tones at 2.25, 6.25 and 10.25 s on the *container* clock.
                # The audio stream itself starts 1.25 s after the video.
                execute([
                    str(ffmpeg), "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=11",
                    "-itsoffset", "1.25", "-f", "lavfi", "-i",
                    r"aevalsrc=0.5*sin(2*PI*997*t)*between(mod(t\,4)\,1\,1.25):s=48000:d=9.75",
                    "-c:v", video_codec, "-c:a", audio_codec, str(source),
                ])
                model = root / "fake-model.bin"
                model.write_bytes(b"not a model")
                measured_durations = []

                def command(args):
                    if args[0] == str(ffmpeg):
                        execute(args)
                        return
                    self.assertEqual("fake-whisper", args[0])
                    wav_path = Path(args[args.index("-f") + 1])
                    with wave.open(str(wav_path), "rb") as wav:
                        self.assertEqual((1, 16000, 2),
                                         (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()))
                        samples = array.array("h", wav.readframes(wav.getnframes()))
                    measured_durations.append(len(samples) / 16000)
                    active = [i / 16000 for i in range(0, len(samples) - 159, 160)
                              if math.sqrt(sum(float(v) ** 2 for v in samples[i:i+160]) / 160) > 1000]
                    self.assertTrue(active, "the requested chunk must retain its tone")
                    prefix = Path(args[args.index("-of") + 1])
                    chunks.write_srt(prefix.with_suffix(".srt"), [
                        chunks.Block(active[0], active[-1] + .01, ["tone"]),
                    ])
                    prefix.with_suffix(".txt").write_text("tone", encoding="utf-8")

                argv = ["transcribe", "--input-video", str(source), "--first-audio",
                        "--output-srt", str(root / "A.srt"),
                        "--raw-output-srt", str(root / "A.raw.srt"),
                        "--output-txt", str(root / "A.txt"),
                        "--work-dir", str(root / "work"),
                        "--ffmpeg", str(ffmpeg), "--ffprobe", str(ffprobe),
                        "--whisper", "fake-whisper", "--model", str(model),
                        "--chunk-seconds", "4", "--duration-seconds", "11"]
                with patch.object(sys, "argv", argv), \
                     patch.object(chunks, "run_command", side_effect=command), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(0, chunks.main())

                self.assertEqual([4.0, 4.0, 3.0], measured_durations)
                cues = chunks.parse_srt(root / "A.srt", 0)
                self.assertEqual(3, len(cues))
                for cue, expected in zip(cues, (2.25, 6.25, 10.25)):
                    self.assertAlmostEqual(expected, cue.start, delta=.10)


if __name__ == "__main__":
    unittest.main()
