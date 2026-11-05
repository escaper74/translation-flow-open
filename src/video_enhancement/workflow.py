"""Bounded-frame local SR with resumable video chunks and original timestamps.

Decode once, normalize VFR to the source's nominal rate, and feed small batches
to the standalone image model. Only encoded chunks are durable checkpoints.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading

from translation_flow.cancellation import check_cancelled, run_asr_process
from .runtime import MODEL, NOMOS_MODEL, check_runtime, model_root, model_spec

BATCH_FRAMES = 64


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def identity(path):
    stat = path.stat()
    return dict(path=str(path.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


@dataclass(frozen=True)
class SRConfig:
    source: Path
    work_dir: Path
    output: Path
    ffmpeg: Path
    ffprobe: Path
    max_height: int = 1080
    preview_start: float = 0
    preview_duration: float | None = None
    runtime: Path | None = None
    model: str = MODEL


def run_command(args, cancelled, *, error_log=None):
    result = run_asr_process([str(a) for a in args], cancelled=cancelled)
    if result.returncode:
        # Native diagnostics stay local; do not put commands or media paths in
        # public events. Keep the useful tail for driver/model failure diagnosis.
        if error_log is not None:
            Path(error_log).write_text(result.stderr[-8192:], encoding="utf-8")
        log_hint = ' 原因见任务目录的 runtime-error.log。' if error_log is not None else ''
        raise RuntimeError(f"{Path(args[0]).stem} 执行失败（代码 {result.returncode}）；已保留完成段。" + log_hint)
    return result.stdout


def probe(ffprobe, path, cancelled):
    return json.loads(run_command([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                   "-of", "json", path], cancelled))


def source_plan(config, cancelled):
    data = probe(config.ffprobe, config.source, cancelled)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("所选文件没有视频轨。")
    if video.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise ValueError("当前轻量超分支持 SDR 视频；HDR 色彩转换尚未接入。")
    fps = Fraction(0)
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = Fraction(video.get(key) or "0/1")
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= fps <= 240:
            break
    if not 1 <= fps <= 240:
        raise ValueError("无法确定可用的视频帧率。")
    # FFmpeg without -copyts uses the container start as the timeline origin.
    container_start = float(data.get("format", {}).get("start_time", 0))
    video_start = max(0.0, float(video.get("start_time", container_start)) - container_start)
    start = video_start + config.preview_start
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= start:
        raise ValueError("短片起点超出视频时长。")
    end = min(duration, start + config.preview_duration) if config.preview_duration else duration
    # Align the selected interval to the one global CFR grid; audio uses this
    # actual start, never the requested seek time or the elapsed wall time.
    first_frame = math.ceil(start * fps - 1e-6)
    origin = float(first_frame / fps)
    sar_text = video.get("sample_aspect_ratio")
    sar = Fraction((sar_text if sar_text and sar_text != "N/A" else "1:1").replace(":", "/"))
    width, height = round(int(video["width"]) * (sar or 1)), int(video["height"])
    rotation = next((abs(int(s.get("rotation", 0))) % 180 for s in video.get("side_data_list", [])
                     if "rotation" in s), 0)
    if rotation == 90:
        width, height = height, width
    factor = min(2.0, config.max_height / height)
    out_height = max(2, round(height * max(1, factor) / 2) * 2)
    out_width = max(2, round(width * max(1, factor) / 2) * 2)
    return dict(fps=str(fps), source_origin_seconds=origin, source_video_origin_seconds=video_start, end_seconds=end,
                first_frame=first_frame, end_frame=math.ceil(end * fps - 1e-6),
                width=out_width, height=out_height, needs_upscale=factor > 1,
                frame_rate_policy="source_average_cfr_no_interpolation")


def read_png(stream):
    signature = stream.read(8)
    if not signature:
        return None
    if signature != b"\x89PNG\r\n\x1a\n":
        raise ValueError("视频解码未返回完整 PNG 帧。")
    blocks = [signature]
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("视频帧被截断。")
        length, kind = struct.unpack(">I4s", header)
        body = stream.read(length + 4)
        if len(body) != length + 4:
            raise ValueError("视频帧数据不完整。")
        blocks.extend((header, body))
        if kind == b"IEND":
            return b"".join(blocks)


@contextmanager
def decoded_frames(config, plan, skip, cancelled):
    filters = (f"scale=trunc(iw*sar/2)*2:ih,setsar=1,fps={plan['fps']}:start_time=0,"
               f"trim=start_frame={plan['first_frame']}:end_frame={plan['end_frame']},"
               f"setpts=PTS-STARTPTS,trim=start_frame={skip}")
    command = [str(config.ffmpeg), "-v", "error", "-nostdin", "-i", str(config.source),
               "-map", "0:v:0", "-an", "-sn", "-vf", filters, "-fps_mode", "passthrough",
               "-c:v", "png", "-f", "image2pipe", "pipe:1"]
    packets, closing = queue.Queue(maxsize=2), threading.Event()
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as child:
        def publish(item):
            while not closing.is_set():
                try:
                    packets.put(item, timeout=0.2)
                    return
                except queue.Full:
                    pass

        def reader():
            try:
                while not closing.is_set():
                    frame = read_png(child.stdout)
                    publish(frame)
                    if frame is None:
                        return
            except Exception as error:
                publish(error)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        def frames():
            while True:
                check_cancelled(cancelled)
                try:
                    item = packets.get(timeout=0.2)
                except queue.Empty:
                    continue
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    if child.wait() != 0:
                        raise RuntimeError("视频解码失败；已保留超分完成段。")
                    return
                yield item
        try:
            yield frames()
        finally:
            closing.set()
            if child.poll() is None:
                child.kill()
            child.wait()
            thread.join(timeout=5)


def native_upscale(input_dir, output_dir, plan, config, cancelled):
    spec = model_spec(config.model)
    runtime = model_root(config.runtime, config.model)
    check_runtime(config.runtime, config.model)
    if config.model == NOMOS_MODEL:
        # This compatible NCNN binary runs the author's original Compact graph,
        # including Clip. Output is natively 2x; the shared encoder sizes it.
        run_command([runtime / spec["executable"], "-i", input_dir, "-o", output_dir,
                     "-m", runtime / "models", "-n", spec["weights"], "-s", "2",
                     "-t", "0", "-g", "0", "-j", "4:4:4", "-f", "png"],
                    cancelled, error_log=config.work_dir / 'runtime-error.log')
        return
    # Upscayl skips both output resizers when -s and -r are combined. Use only
    # the exact target size so 4x intermediate PNGs never leave the engine.
    run_command([runtime / "upscayl-bin.exe", "-i", input_dir, "-o", output_dir,
                 "-m", runtime / "models", "-n", MODEL, "-z", "4",
                 "-r", f"{plan['width']}x{plan['height']}", "-t", "0", "-j", "1:2:2",
                 "-f", "png"], cancelled, error_log=config.work_dir / 'runtime-error.log')


def run_super_resolution(config, *, cancelled=lambda: False, on_progress=None,
                         upscale=native_upscale):
    check_cancelled(cancelled)
    root = config.work_dir
    root.mkdir(parents=True, exist_ok=True)
    config.output.parent.mkdir(parents=True, exist_ok=True)
    spec = model_spec(config.model)
    batch_frames = 256 if config.model == NOMOS_MODEL else BATCH_FRAMES
    plan = source_plan(config, cancelled)
    manifest = dict(source_identity=identity(config.source), output=str(config.output),
                    max_height=config.max_height, preview_start=config.preview_start,
                    preview_duration=config.preview_duration, runtime=spec["version"],
                    batch_frames=batch_frames, plan=plan)
    # Keep the original Lite manifest exactly compatible with stopped tasks.
    if config.model != MODEL:
        manifest["model"] = config.model
    manifest_path, report_path = root / "manifest.json", root / "report.json"
    if manifest_path.exists():
        if read_json(manifest_path) != manifest:
            raise ValueError("超分源片或选项已改变，请建立新任务；原检查点已保留。")
    else:
        write_json(manifest_path, manifest)
    if report_path.is_file() and config.output.is_file():
        report = read_json(report_path)
        if report.get("output_identity") == identity(config.output):
            return dict(report, reused=True)
        raise ValueError("超分产物已被替换；保留原报告，请建立新任务。")
    if config.output.exists():
        raise ValueError("目标视频已存在但缺少有效报告；不会覆盖。")

    if not plan['needs_upscale'] and config.preview_duration is None:
        # Already at the requested height: no model, PNG expansion, or lossy
        # re-encoding. Preserve the original video and normalize its origin.
        origin = plan['source_video_origin_seconds']
        with tempfile.TemporaryDirectory(prefix='passthrough-', dir=root) as scratch:
            pending = Path(scratch) / 'video.mkv'
            run_command([config.ffmpeg, '-v', 'error', '-nostdin', '-n', '-i', config.source,
                         '-map', '0:v:0', '-an', '-sn', '-c:v', 'copy',
                         '-output_ts_offset', str(-origin),
                         '-avoid_negative_ts', 'disabled', pending], cancelled)
            actual = probe(config.ffprobe, pending, cancelled)
            video = next(s for s in actual['streams'] if s['codec_type'] == 'video')
            if abs(float(video.get('start_time', 0))) > 0.1:
                raise ValueError('原画直通的时间起点无法匹配，保留原片。')
            duration = float(actual['format']['duration'])
            pending.rename(config.output)
        report = dict(status='technical_checks_passed', scope='full', output=str(config.output),
                      output_identity=identity(config.output), source_identity=manifest['source_identity'],
                      source_origin_seconds=origin, final_duration_seconds=duration,
                      segment_mappings=[dict(index=0, source_start_seconds=origin, source_end_seconds=origin+duration,
                                             output_start_seconds=0, output_end_seconds=duration)],
                      output_seam_positions_seconds=[], route={**plan, 'engine': 'video_copy', 'source_origin_seconds': origin,
                          'frame_rate_policy': 'original', 'width': video['width'], 'height': video['height']},
                      report=str(report_path), reused=False, human_review_status='pending')
        write_json(report_path, report)
        return report

    chunks, done = [], 0
    for index in range(1000000):
        chunk = root / f"chunk-{index:06d}.mkv"
        record = chunk.with_suffix(".json")
        if not record.exists():
            break
        saved = read_json(record)
        if saved.get("output_identity") != identity(chunk):
            raise ValueError("超分完成段已改变；保留现场。")
        chunks.append((chunk, saved["frames"]))
        done += saved["frames"]
    if on_progress:
        on_progress(done)
    if plan["needs_upscale"] and upscale is native_upscale:
        check_runtime(config.runtime, config.model)

    # All disposable PNGs and incomplete encodes belong to this invocation.
    # Durable encoded chunks stay outside this temporary directory.
    with decoded_frames(config, plan, done, cancelled) as frames:
        exhausted = False
        while not exhausted:
            check_cancelled(cancelled)
            with tempfile.TemporaryDirectory(prefix="frames-", dir=root) as scratch:
                scratch = Path(scratch)
                originals, enhanced = scratch / "in", scratch / "out"
                originals.mkdir()
                enhanced.mkdir()
                count = 0
                for index in range(batch_frames):
                    frame = next(frames, None)
                    if frame is None:
                        exhausted = True
                        break
                    (originals / f"{index:08d}.png").write_bytes(frame)
                    count += 1
                if not count:
                    break
                if plan["needs_upscale"]:
                    upscale(originals, enhanced, plan, config, cancelled)
                    if {p.name for p in enhanced.glob("*.png")} != {p.name for p in originals.glob("*.png")}:
                        raise ValueError("超分返回的帧不完整；不会发布本段。")
                else:
                    enhanced = originals
                pending = scratch / "encoded.mkv"
                run_command([config.ffmpeg, "-v", "error", "-nostdin", "-n", "-framerate", plan["fps"],
                             "-i", enhanced / "%08d.png", "-frames:v", str(count), "-an", "-sn",
                             "-vf", f"scale={plan['width']}:{plan['height']},setsar=1", "-c:v", "libx264",
                             "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", pending], cancelled)
                chunk = root / f"chunk-{len(chunks):06d}.mkv"
                # An interrupted publish may have a valid chunk but no record.
                # Keep it, and use an exclusive alternative rather than overwrite.
                if chunk.exists():
                    raise ValueError("发现未记录的超分视频段，已保留现场；请使用新任务。")
                pending.rename(chunk)
                write_json(chunk.with_suffix(".json"), dict(frames=count, output_identity=identity(chunk)))
                chunks.append((chunk, count))
                done += count
                if on_progress:
                    on_progress(done)
    if not chunks:
        raise ValueError("所选时间窗口没有可处理画面。")
    check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix="assemble-", dir=root) as scratch:
        scratch = Path(scratch)
        listing = scratch / "concat.txt"
        # Names are generated above; no source path is interpolated in ffconcat.
        listing.write_text("".join(f"file '../{path.name}'\nduration {float(count / Fraction(plan['fps'])):.9f}\n"
                                   for path, count in chunks), encoding="utf-8")
        pending = scratch / "video.mkv"
        run_command([config.ffmpeg, "-v", "error", "-nostdin", "-n", "-f", "concat", "-safe", "0",
                     "-i", listing, "-map", "0:v:0", "-c", "copy", pending], cancelled)
        actual = probe(config.ffprobe, pending, cancelled)
        video = next(s for s in actual["streams"] if s["codec_type"] == "video")
        expected_duration = float(done / Fraction(plan["fps"]))
        actual_duration = float(actual["format"]["duration"])
        if (video["width"] != plan["width"] or video["height"] != plan["height"] or
                abs(actual_duration - expected_duration) > 0.25):
            raise ValueError("超分拼接的尺寸或时长不匹配；保留完成段。")
        run_command([config.ffmpeg, "-v", "error", "-nostdin", "-i", pending,
                     "-frames:v", "1", "-f", "null", "-"], cancelled)
        # rename is exclusive on Windows; source and output are never overwritten.
        pending.rename(config.output)
    cursor, mappings, seams = 0.0, [], []
    for index, (_, count) in enumerate(chunks):
        end = cursor + float(count / Fraction(plan["fps"]))
        mappings.append(dict(index=index, source_start_seconds=plan["source_origin_seconds"] + cursor,
                             source_end_seconds=plan["source_origin_seconds"] + end,
                             output_start_seconds=cursor, output_end_seconds=end))
        if index:
            seams.append(cursor)
        cursor = end
    report = dict(status="technical_checks_passed", scope="full", output=str(config.output),
                  output_identity=identity(config.output), source_identity=manifest["source_identity"],
                  source_origin_seconds=plan["source_origin_seconds"], final_duration_seconds=actual_duration,
                  segment_mappings=mappings, output_seam_positions_seconds=seams, frames=done,
                  route=dict(engine=spec["version"], model=spec["weights"], model_label=spec["label"], **plan),
                  report=str(report_path),
                  reused=False, human_review_status="pending")
    write_json(report_path, report)
    return report
