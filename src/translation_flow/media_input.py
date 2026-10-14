"""Small local input checks before spending time on ASR or text services."""

from __future__ import annotations

import math
from pathlib import Path
import subprocess
from typing import Any, Callable
import uuid

from media_planning.source import FFprobeSourceProbe, parse_ffprobe_payload


def check_media_input(source: Path, work_dir: Path, ffmpeg: Path, ffprobe: Path,
                      *, process_runner: Callable[..., Any] = subprocess.run,
                      first_audio: bool = True) -> dict[str, Any]:
    """Check actual streams, first-audio decoding and original MKV compatibility.

    Never transcode a full video or alter the source. The short mux is disposable
    diagnostic output owned by this invocation, not a production checkpoint.
    """
    source = source.resolve()
    payload = FFprobeSourceProbe(ffprobe, process_runner=process_runner).probe_payload(source)
    facts = parse_ffprobe_payload(payload)
    duration = facts.duration_seconds
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("媒体没有可用的正时长，不能启动字幕识别")
    if not facts.video_candidates:
        raise ValueError("媒体缺少可用的视频轨（封面图不算视频）")
    if not facts.audio_streams:
        raise ValueError("媒体缺少音轨，无法进行语音识别；已有字幕可使用独立 SRT 入口")
    if any(not stream.codec_name for stream in (facts.video_candidates[0], *facts.audio_streams)):
        raise ValueError("媒体有无法识别编码的主要轨道，不能保证识别或原画封装")

    def run(args: list[str], label: str) -> None:
        result = process_runner(args, check=False, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise ValueError(f"{label}未通过，尚未启动 ASR/翻译：{detail}")

    # Both Whisper engines consume audio ordinal zero. Preserve every original
    # audio track in final delivery regardless of the one used for recognition.
    prefix = [str(ffmpeg), "-v", "error", "-xerror", "-nostdin", "-n", "-i", str(source)]
    run([*prefix, "-map", "0:a:0", "-t", "1", "-vn", "-sn", "-dn",
         "-ac", "1", "-ar", "16000", "-f", "null", "-"], "首条音轨短解码")
    work_dir.mkdir(parents=True, exist_ok=True)
    candidate = work_dir / f".input-check-{uuid.uuid4().hex}.mkv"
    try:
        run([*prefix, "-map", "0:V:0", "-map", "0:a", "-t", "1", "-sn", "-dn",
             "-c:v", "copy", "-c:a", "copy", str(candidate)], "原画与全部音轨的 MKV 兼容检查")
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise ValueError("MKV 兼容检查未生成有效短样，尚未启动 ASR/翻译")
    finally:
        candidate.unlink(missing_ok=True)
    stat = source.stat()
    warnings = []
    if len(facts.audio_streams) > 1:
        warnings.append("新任务识别使用首条音轨；成片保留全部原音轨。请确认首条音轨是需要翻译的对白。"
                        if first_audio else "旧任务恢复保留 Whisper A 的原自动选轨方式，C 仍使用首条音轨；本次不混入新的 A 选轨策略。")
    if len(facts.video_candidates) > 1:
        warnings.append("原画字幕成片使用首条非封面视频轨；其他视频轨不进入成片。")
    return {"status": "passed", "source": str(source), "source_size_bytes": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns, "format_names": list(facts.format_names),
            "duration_seconds": duration, "video_index": facts.video_candidates[0].index,
            "video_codec": facts.video_candidates[0].codec_name,
            "asr_audio_index": facts.audio_streams[0].index if first_audio else None,
            "asr_audio_selection": "first" if first_audio else "legacy",
            "audio_indices": [s.index for s in facts.audio_streams],
            "audio_codecs": [s.codec_name for s in facts.audio_streams],
            "attached_picture_indices": [s.index for s in facts.attached_pictures],
            "checks": {"audio_short_decode": True, "mkv_short_copy_mux": True},
            "warnings": warnings}
