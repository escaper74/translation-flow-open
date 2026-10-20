from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from translation_flow.packaging import MkvSoftSubtitlePackager


@unittest.skipUnless(
    os.environ.get("TRANSLATION_FLOW_TOOL_ROOT"),
    "set TRANSLATION_FLOW_TOOL_ROOT for local FFmpeg packaging acceptance",
)
class TranslationPackagingSyntheticTests(unittest.TestCase):
    def test_keeps_one_video_all_audio_and_only_new_chinese_subtitle(self) -> None:
        tool_root = Path(os.environ["TRANSLATION_FLOW_TOOL_ROOT"])
        binary_dir = (
            tool_root
            / "ffmpeg"
            / "ffmpeg-8.1.1-essentials_build"
            / "bin"
        )
        ffmpeg = binary_dir / "ffmpeg.exe"
        ffprobe = binary_dir / "ffprobe.exe"
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest("configured FFmpeg tools are missing")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_srt = root / "old.srt"
            new_srt = root / "new.zh-CN.srt"
            source = root / "source-with-old-subtitle.mkv"
            output = root / "final.mkv"
            report = root / "technical.json"
            old_srt.write_text(
                "1\n00:00:00,100 --> 00:00:03,500\nold subtitle\n",
                encoding="utf-8",
            )
            new_srt.write_text(
                "1\n00:00:00,100 --> 00:00:03,500\n新的中文字幕\n",
                encoding="utf-8",
            )
            command = [
                str(ffmpeg),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=10:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=4",
                "-f",
                "srt",
                "-i",
                str(old_srt),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:a:0",
                "-map",
                "3:0",
                "-t",
                "4",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                str(source),
            ]
            generated = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)

            MkvSoftSubtitlePackager(ffmpeg, ffprobe).package(
                source, new_srt, output, report
            )

            probe = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(output),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            self.assertEqual(1, sum(item["codec_type"] == "video" for item in streams))
            self.assertEqual(2, sum(item["codec_type"] == "audio" for item in streams))
            subtitles = [item for item in streams if item["codec_type"] == "subtitle"]
            self.assertEqual(1, len(subtitles))
            self.assertEqual("zho", subtitles[0]["tags"]["language"])
            self.assertEqual(1, subtitles[0]["disposition"]["default"])
            self.assertEqual("passed", json.loads(report.read_text(encoding="utf-8"))["status"])


if __name__ == "__main__":
    unittest.main()
