from __future__ import annotations

import json
import math
import os
import ctypes
import shutil
import sys
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, TextIO

from .acceptance import validate_output, write_report
from .config import (
    DEFAULT_RIFE_CHUNK_FRAMES,
    FINAL_CRF,
    FINAL_PIXEL_FORMAT,
    FINAL_PRESET,
    FINAL_VIDEO_CODEC,
    LADA_DETECTION_MODEL,
    LADA_DEVICE,
    LADA_INTERMEDIATE_LEVEL,
    LADA_MAX_CLIP_LENGTH,
    LADA_RESTORATION_MODEL,
    PIPELINE_VERSION,
    QUALITY_DENOISE_FILTER,
    QUALITY_FINAL_CAS_FILTER,
    RIFE_GPU_ID,
    RIFE_MODEL,
    RIFE_THREADS,
    PipelineOptions,
    build_execution_plan,
)
from .events import EventEmitter
from .media import (
    decoded_frame_count,
    duration_of,
    first_stream,
    parse_fraction,
    probe_media,
    run_logged_process,
)
from .safety import safe_remove_tree, safe_unlink
from .state import (
    JobState,
    canonical_hash,
    fingerprint_file,
    fingerprint_matches,
    sha256_file,
    utc_now,
)
from .toolchain import TemporaryAsciiMappings, Toolchain


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineRequest:
    input_video: Path
    output_video: Path
    options: PipelineOptions
    tool_root: Path
    job_dir: Path | None = None
    cleanup_on_success: bool = True
    rife_chunk_frames: int = DEFAULT_RIFE_CHUNK_FRAMES


class VideoPipelineRunner:
    def __init__(self, request: PipelineRequest, *, stdout: TextIO = sys.stdout) -> None:
        self.request = request
        self.stdout = stdout
        self.input_video = request.input_video.expanduser().resolve()
        self.output_video = request.output_video.expanduser().resolve()
        self.job_dir = (
            request.job_dir.expanduser().resolve()
            if request.job_dir
            else self.output_video.parent
            / f".{self.output_video.name}.video-pipeline-job"
        )
        self.logs_dir = self.job_dir / "logs"
        self.work_dir = self.job_dir / "work"
        self.artifacts_dir = self.job_dir / "artifacts"
        self.state_path = self.job_dir / "job.json"
        self.ownership_marker = self.job_dir / ".video-pipeline-owned"
        self.lock_path = self.job_dir / ".video-pipeline-running.lock"
        self.lock_token: str | None = None
        self.report_path = self.job_dir / "acceptance.json"
        self.events_path = self.job_dir / "events.jsonl"
        self.toolchain = Toolchain.from_root(request.tool_root)
        self.state: JobState | None = None
        self.emitter: EventEmitter | None = None
        self.job_id = self._existing_or_new_job_id()

    def _existing_or_new_job_id(self) -> str:
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if data.get("job_id"):
                    return str(data["job_id"])
            except (OSError, json.JSONDecodeError):
                pass
        return str(uuid.uuid4())

    def run(self) -> dict[str, Any]:
        try:
            self._prepare_job_directory()
            self._acquire_job_lock()
        except Exception as exc:
            line = json.dumps(
                {
                    "event": "job_failed",
                    "timestamp_utc": utc_now(),
                    "job_id": self.job_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "job_dir": str(self.job_dir),
                    "persisted": False,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            self.stdout.write(line + "\n")
            self.stdout.flush()
            raise
        try:
            self.emitter = EventEmitter(self.events_path, self.stdout)
            self.emitter.emit(
                "job_started",
                job_id=self.job_id,
                pipeline_version=PIPELINE_VERSION,
                input_video=str(self.input_video),
                output_video=str(self.output_video),
                job_dir=str(self.job_dir),
                options=self.request.options.to_dict(),
            )
            try:
                return self._run_inner()
            except Exception as exc:
                if self.state is not None:
                    self.state.set_status(
                        "failed",
                        failed_at_utc=utc_now(),
                        error={"type": type(exc).__name__, "message": str(exc)},
                    )
                recovery_hint = (
                    "保留 job.json、events.jsonl、日志和已完成恢复点；修正问题后用完全相同参数重跑。"
                    if self.state is not None
                    else "保留任务目录标记和 events.jsonl；环境恢复后用完全相同参数及 JobDir 重跑。"
                )
                self.emitter.emit(
                    "job_failed",
                    job_id=self.job_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    job_dir=str(self.job_dir),
                    recovery_hint=recovery_hint,
                )
                raise
        finally:
            self._release_job_lock()

    def _prepare_job_directory(self) -> None:
        if not self.input_video.is_file():
            raise FileNotFoundError(f"输入视频不存在: {self.input_video}")
        if self.output_video.suffix.casefold() != ".mkv":
            raise PipelineError("正式输出当前只接受 .mkv，以稳定复制原音频")
        if self.input_video == self.output_video:
            raise PipelineError("输出路径不得与源视频相同")
        self.output_video.parent.mkdir(parents=True, exist_ok=True)
        if self.job_dir.exists() and not self.job_dir.is_dir():
            raise PipelineError(f"任务路径不是目录: {self.job_dir}")
        if self.job_dir.exists() and not self.state_path.exists():
            if any(self.job_dir.iterdir()) and not self.ownership_marker.is_file():
                raise PipelineError(
                    f"任务目录非空但没有 job.json，拒绝接管: {self.job_dir}"
                )
        self.job_dir.mkdir(parents=True, exist_ok=True)
        if not self.ownership_marker.exists():
            self.ownership_marker.write_text(
                "video_pipeline owned job directory\n",
                encoding="ascii",
                newline="\n",
            )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.input_video.relative_to(self.job_dir)
        except ValueError:
            pass
        else:
            raise PipelineError("源视频不得位于任务临时目录内")
        if self.output_video.drive.casefold() != self.job_dir.drive.casefold():
            raise PipelineError("任务目录与输出必须在同一盘符，才能安全原子交付")
        if self.request.rife_chunk_frames < 30:
            raise PipelineError("RIFE 分块至少需要 30 个源帧")

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _acquire_job_lock(self) -> None:
        token = uuid.uuid4().hex
        payload = {
            "pid": os.getpid(),
            "token": token,
            "job_id": self.job_id,
            "acquired_at_utc": utc_now(),
        }
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        for _ in range(3):
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    existing_pid = int(existing.get("pid", -1))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    existing_pid = -1
                if self._process_is_alive(existing_pid):
                    raise PipelineError(
                        f"任务目录正在被进程 {existing_pid} 使用，拒绝并发启动"
                    )
                stale = self.lock_path.with_name(
                    f"{self.lock_path.name}.stale-{uuid.uuid4().hex}.json"
                )
                try:
                    os.replace(self.lock_path, stale)
                except FileNotFoundError:
                    continue
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.lock_token = token
            return
        raise PipelineError("无法取得任务目录独占锁")

    def _release_job_lock(self) -> None:
        if self.lock_token is None:
            return
        try:
            existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError("任务锁身份无法确认，拒绝解除") from exc
        if existing.get("token") != self.lock_token or existing.get("pid") != os.getpid():
            raise PipelineError("任务锁身份不匹配，拒绝解除")
        safe_unlink(self.lock_path, self.job_dir)
        self.lock_token = None

    def _run_inner(self) -> dict[str, Any]:
        assert self.emitter is not None
        options = self.request.options
        if self.output_video.exists() and not self.state_path.exists():
            raise FileExistsError(f"输出已经存在，拒绝覆盖: {self.output_video}")

        self.emitter.emit(
            "stage_started", job_id=self.job_id, stage="preflight", sequence=0
        )
        self.toolchain.validate(
            need_lada=options.enable_demosaic,
            need_rife=options.enable_frame_interpolation_2x,
        )
        input_probe = probe_media(self.toolchain.ffprobe, self.input_video)
        input_video_stream = first_stream(input_probe, "video")
        duration = duration_of(input_probe)
        frame_rate = parse_fraction(
            input_video_stream.get("avg_frame_rate")
            or input_video_stream.get("r_frame_rate")
        )
        width = int(input_video_stream["width"])
        height = int(input_video_stream["height"])
        input_fingerprint = fingerprint_file(self.input_video)
        tool_versions = self.toolchain.inspect_versions(
            runtime_dir=self.work_dir / "tool-runtime",
            need_lada=options.enable_demosaic,
            need_rife=options.enable_frame_interpolation_2x,
        )
        execution_plan = build_execution_plan(options)
        implementation_fingerprint = self._implementation_fingerprint()
        normalized_request = {
            "input_video": str(self.input_video),
            "output_video": str(self.output_video),
            "job_dir": str(self.job_dir),
            "tool_root": str(self.toolchain.root),
            "options": options.to_dict(),
            "cleanup_on_success": self.request.cleanup_on_success,
            "rife_chunk_frames": self.request.rife_chunk_frames,
            "execution_plan": execution_plan,
        }
        cache_material = {
            "pipeline_version": PIPELINE_VERSION,
            "input_fingerprint": input_fingerprint,
            "request": normalized_request,
            "tool_versions": {
                key: value
                for key, value in tool_versions.items()
                if key not in {"nvidia_gpu_samples", "nvidia_gpu_engine_samples"}
            },
            "implementation_fingerprint": implementation_fingerprint,
        }
        cache_key = canonical_hash(cache_material)
        self.state, resumed = JobState.create_or_load(
            self.state_path,
            job_id=self.job_id,
            cache_key=cache_key,
            request=normalized_request,
        )
        self.job_id = str(self.state.data["job_id"])
        self.state.data["input_fingerprint"] = input_fingerprint
        self.state.data["tool_versions"] = tool_versions
        self.state.data["implementation_fingerprint"] = implementation_fingerprint
        self.state.data["media_input_spec"] = {
            "width": width,
            "height": height,
            "duration_seconds": duration,
            "frame_rate": f"{frame_rate.numerator}/{frame_rate.denominator}",
        }

        if self.output_video.exists():
            completed_output = self.state.data.get("output")
            if self.state.data.get("status") == "completed" and fingerprint_matches(
                completed_output
            ):
                self.emitter.emit(
                    "stage_completed",
                    job_id=self.job_id,
                    stage="preflight",
                    reused=True,
                    resumed=resumed,
                )
                self.emitter.emit(
                    "job_completed",
                    job_id=self.job_id,
                    output_video=str(self.output_video),
                    output_sha256=completed_output["sha256"],
                    acceptance_report=str(self.report_path),
                    reused=True,
                    visual_acceptance="not_performed",
                )
                return self.state.data
            raise FileExistsError(f"输出已经存在且不属于已完成任务，拒绝覆盖: {self.output_video}")

        disk = self._disk_preflight(
            input_bytes=input_fingerprint["bytes"],
            duration=duration,
            width=width,
            height=height,
            frame_rate=frame_rate,
        )
        self.state.data["disk_preflight"] = disk
        self.state.set_status("running", resumed=resumed)
        if options.enable_frame_interpolation_2x:
            self._rife_smoke_test()
        self.state.complete_stage(
            "preflight",
            input_fingerprint=input_fingerprint,
            tool_versions=tool_versions,
            disk=disk,
        )
        self.emitter.emit(
            "stage_completed",
            job_id=self.job_id,
            stage="preflight",
            resumed=resumed,
            disk=disk,
        )

        interpolation_chunks = self._reusable_interpolation_chunks()
        current = self.input_video
        upstream_artifacts: list[Path] = []
        if interpolation_chunks is None:
            quality_artifact = (
                self.state.reusable_artifact("quality_enhancement")
                if options.enable_quality_enhancement
                else None
            )
            if quality_artifact is not None:
                self._emit_reused("quality_enhancement", quality_artifact)
                current = quality_artifact
            else:
                if options.enable_demosaic:
                    demosaic_artifact = self.state.reusable_artifact("demosaic")
                    if demosaic_artifact is None:
                        demosaic_artifact = self._run_demosaic(current, duration)
                    else:
                        self._emit_reused("demosaic", demosaic_artifact)
                    current = demosaic_artifact
                    upstream_artifacts.append(demosaic_artifact)
                if options.enable_quality_enhancement:
                    quality_artifact = self._run_quality_enhancement(current, duration)
                    current = quality_artifact
                    upstream_artifacts.append(quality_artifact)
                    if (
                        options.enable_demosaic
                        and self.request.cleanup_on_success
                        and len(upstream_artifacts) >= 2
                    ):
                        self._cleanup_file(
                            upstream_artifacts[-2],
                            "已由低强度降噪 FFV1 恢复点取代",
                        )
            if options.enable_frame_interpolation_2x:
                interpolation_chunks = self._run_interpolation(
                    current=current,
                    duration=duration,
                    source_rate=frame_rate,
                )
                if self.request.cleanup_on_success and current != self.input_video:
                    self._cleanup_file(current, "已由全部 RIFE 无损分块取代")

        final_candidate = self._run_final_output(
            current=current,
            interpolation_chunks=interpolation_chunks,
            source_rate=frame_rate,
            duration=duration,
        )
        report = self._run_validation(
            final_candidate=final_candidate,
            input_fingerprint=input_fingerprint,
            normalized_request=normalized_request,
        )
        if report["status"] != "passed":
            raise PipelineError(
                "技术验收失败；候选未交付。失败项: " + " | ".join(report["failures"])
            )
        if self.output_video.exists():
            raise FileExistsError(f"验收后发现输出路径已被占用，拒绝覆盖: {self.output_video}")
        os.replace(final_candidate, self.output_video)
        output_fingerprint = fingerprint_file(self.output_video)
        report["delivered_output"] = output_fingerprint
        write_report(self.report_path, report)
        self.state.complete_stage(
            "technical_validation",
            report_path=str(self.report_path),
            report_status="passed",
            output=output_fingerprint,
        )
        self.state.set_status(
            "completed",
            completed_at_utc=utc_now(),
            output=output_fingerprint,
            acceptance_report=str(self.report_path),
            visual_acceptance="not_performed",
        )
        if self.request.cleanup_on_success:
            self._cleanup_success_intermediates(interpolation_chunks)
        self.emitter.emit(
            "job_completed",
            job_id=self.job_id,
            output_video=str(self.output_video),
            output_sha256=output_fingerprint["sha256"],
            acceptance_report=str(self.report_path),
            visual_acceptance="not_performed",
        )
        return self.state.data

    def _disk_preflight(
        self,
        *,
        input_bytes: int,
        duration: float,
        width: int,
        height: int,
        frame_rate: Fraction,
    ) -> dict[str, Any]:
        raw_bytes_per_frame = width * height * 1.5
        source_frames = float(frame_rate) * duration
        ffv1_source_estimate = int(raw_bytes_per_frame * source_frames * 0.25)
        final_estimate = max(input_bytes, int(input_bytes * 1.5))
        required = final_estimate
        if self.request.options.enable_demosaic:
            required += ffv1_source_estimate
        if self.request.options.enable_quality_enhancement:
            required += ffv1_source_estimate
        peak_chunk = 0
        if self.request.options.enable_frame_interpolation_2x:
            rife_chunks_estimate = ffv1_source_estimate * 2
            peak_chunk = int(
                raw_bytes_per_frame
                * (self.request.rife_chunk_frames + 1)
                * 3
                * 3
            )
            required += rife_chunks_estimate + peak_chunk
            if not (
                self.request.options.enable_demosaic
                or self.request.options.enable_quality_enhancement
            ):
                required += ffv1_source_estimate
        reserve = 5 * 1024**3
        required += reserve
        free = shutil.disk_usage(self.job_dir).free
        if free < required:
            raise PipelineError(
                "磁盘空间不足，拒绝开始："
                f"估算至少需要 {required / 1024**3:.1f} GiB，"
                f"当前可用 {free / 1024**3:.1f} GiB"
            )
        return {
            "free_bytes": free,
            "estimated_required_bytes": required,
            "estimated_peak_frame_bytes": peak_chunk,
            "reserve_bytes": reserve,
            "estimation_is_conservative": True,
        }

    @staticmethod
    def _implementation_fingerprint() -> dict[str, Any]:
        package_root = Path(__file__).resolve().parent
        repo_root = package_root.parent.parent
        files = sorted(package_root.glob("*.py"))
        wrapper = repo_root / "scripts" / "run-video-processing.ps1"
        if wrapper.is_file():
            files.append(wrapper)
        entries = [
            {
                "path": path.relative_to(repo_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        ]
        return {"files": entries, "tree_sha256": canonical_hash(entries)}

    def _progress(self, stage: str):
        def callback(percent: float, detail: str) -> None:
            assert self.emitter is not None
            self.emitter.emit(
                "stage_progress",
                job_id=self.job_id,
                stage=stage,
                percent=round(percent, 2),
                detail=detail,
            )

        return callback

    def _emit_reused(self, stage: str, artifact: Path) -> None:
        assert self.emitter is not None
        self.emitter.emit(
            "stage_completed",
            job_id=self.job_id,
            stage=stage,
            reused=True,
            artifact=str(artifact),
        )

    def _attempt_dir(self, stage: str) -> Path:
        path = self.work_dir / stage / f"attempt-{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        assert self.state is not None
        self.state.record_attempt(stage, path, "created")
        return path

    def _validate_intermediate(self, path: Path, reference: Path) -> None:
        reference_probe = probe_media(self.toolchain.ffprobe, reference)
        output_probe = probe_media(self.toolchain.ffprobe, path)
        reference_video = first_stream(reference_probe, "video")
        output_video = first_stream(output_probe, "video")
        if (reference_video.get("width"), reference_video.get("height")) != (
            output_video.get("width"),
            output_video.get("height"),
        ):
            raise PipelineError(f"中间产物分辨率变化: {path}")
        if abs(duration_of(reference_probe) - duration_of(output_probe)) > 0.5:
            raise PipelineError(f"中间产物时长偏差超过 0.5 秒: {path}")

    def _run_demosaic(self, current: Path, duration: float) -> Path:
        assert self.state is not None and self.emitter is not None
        stage = "demosaic"
        self.state.start_stage(stage)
        self.emitter.emit("stage_started", job_id=self.job_id, stage=stage)
        attempt = self._attempt_dir(stage)
        output = attempt / "lada-basicvsrpp-v1.2.ffv1.mkv"
        temp = attempt / "temporary"
        temp.mkdir()
        try:
            run_logged_process(
                [
                    str(self.toolchain.lada_cli),
                    "--input",
                    str(current),
                    "--output",
                    str(output),
                    "--temporary-directory",
                    str(temp),
                    "--device",
                    LADA_DEVICE,
                    "--no-fp16",
                    "--mosaic-restoration-model",
                    LADA_RESTORATION_MODEL,
                    "--mosaic-detection-model",
                    LADA_DETECTION_MODEL,
                    "--max-clip-length",
                    str(LADA_MAX_CLIP_LENGTH),
                    "--no-detect-face-mosaics",
                    "--encoder",
                    "ffv1",
                    "--encoder-options",
                    f"-level {LADA_INTERMEDIATE_LEVEL}",
                ],
                cwd=attempt,
                env=self.toolchain.lada_environment(attempt / "runtime"),
                log_path=self.logs_dir / "demosaic.log",
                progress=self._progress(stage),
                duration_seconds=duration,
            )
            if not output.is_file():
                raise PipelineError("Lada 没有生成预期输出")
            self._validate_intermediate(output, current)
            artifact = self.artifacts_dir / f"demosaic-{uuid.uuid4().hex}.ffv1.mkv"
            os.replace(output, artifact)
            fingerprint = fingerprint_file(artifact)
            self.state.complete_stage(stage, artifact=fingerprint)
            safe_remove_tree(attempt, self.job_dir)
            self.state.record_cleanup(attempt, "Lada 临时目录已由无损恢复点取代")
            self.emitter.emit(
                "stage_completed",
                job_id=self.job_id,
                stage=stage,
                artifact=str(artifact),
                sha256=fingerprint["sha256"],
            )
            return artifact
        except Exception as exc:
            self.state.fail_stage(stage, str(exc))
            raise

    def _run_quality_enhancement(self, current: Path, duration: float) -> Path:
        assert self.state is not None and self.emitter is not None
        stage = "quality_enhancement"
        self.state.start_stage(
            stage,
            denoise_filter=QUALITY_DENOISE_FILTER,
            final_cas_filter=QUALITY_FINAL_CAS_FILTER,
        )
        self.emitter.emit("stage_started", job_id=self.job_id, stage=stage)
        attempt = self._attempt_dir(stage)
        output = attempt / "low-denoise.ffv1.mkv"
        try:
            run_logged_process(
                [
                    str(self.toolchain.ffmpeg),
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(current),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-vf",
                    QUALITY_DENOISE_FILTER,
                    "-c:v",
                    "ffv1",
                    "-level",
                    str(LADA_INTERMEDIATE_LEVEL),
                    "-pix_fmt",
                    FINAL_PIXEL_FORMAT,
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(output),
                ],
                log_path=self.logs_dir / "quality-enhancement.log",
                progress=self._progress(stage),
                duration_seconds=duration,
            )
            self._validate_intermediate(output, current)
            artifact = self.artifacts_dir / f"quality-{uuid.uuid4().hex}.ffv1.mkv"
            os.replace(output, artifact)
            fingerprint = fingerprint_file(artifact)
            self.state.complete_stage(stage, artifact=fingerprint)
            safe_remove_tree(attempt, self.job_dir)
            self.state.record_cleanup(attempt, "降噪临时目录已由无损恢复点取代")
            self.emitter.emit(
                "stage_completed",
                job_id=self.job_id,
                stage=stage,
                artifact=str(artifact),
                sha256=fingerprint["sha256"],
                final_cas_deferred=True,
            )
            return artifact
        except Exception as exc:
            self.state.fail_stage(stage, str(exc))
            raise

    def _rife_smoke_test(self) -> None:
        assert self.state is not None
        smoke = self._attempt_dir("rife_gpu_smoke")
        input_dir = smoke / "input"
        output_dir = smoke / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        try:
            run_logged_process(
                [
                    str(self.toolchain.ffmpeg),
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=64x64:r=2:d=1",
                    "-frames:v",
                    "2",
                    "-start_number",
                    "0",
                    str(input_dir / "%08d.png"),
                ],
                log_path=self.logs_dir / "rife-gpu-smoke.log",
            )
            with TemporaryAsciiMappings() as mappings:
                ascii_tool = mappings.map_directory(self.toolchain.rife_dir)
                ascii_smoke = mappings.map_directory(smoke)
                run_logged_process(
                    [
                        str(ascii_tool / self.toolchain.rife_exe.name),
                        "-i",
                        str(ascii_smoke / "input"),
                        "-o",
                        str(ascii_smoke / "output"),
                        "-n",
                        "3",
                        "-m",
                        RIFE_MODEL,
                        "-g",
                        str(RIFE_GPU_ID),
                        "-j",
                        RIFE_THREADS,
                        "-f",
                        "%08d.png",
                    ],
                    cwd=ascii_tool,
                    log_path=self.logs_dir / "rife-gpu-smoke.log",
                )
            if len(list(output_dir.glob("*.png"))) != 3:
                raise PipelineError("RIFE GPU 冒烟检查没有生成 3 帧")
            safe_remove_tree(smoke, self.job_dir)
            self.state.record_cleanup(smoke, "RIFE GPU 冒烟检查通过")
        except Exception:
            raise PipelineError(
                f"RIFE v4.6 GPU/模型冒烟检查失败；证据保留在 {smoke}"
            )

    def _reusable_interpolation_chunks(self) -> list[Path] | None:
        if not self.request.options.enable_frame_interpolation_2x:
            return None
        if self.state is None:
            return None
        stage = self.state.data.get("stages", {}).get("frame_interpolation_2x", {})
        if stage.get("status") != "completed":
            return None
        chunks = stage.get("chunks", [])
        if not chunks or not all(fingerprint_matches(item.get("artifact")) for item in chunks):
            return None
        paths = [Path(item["artifact"]["path"]) for item in sorted(chunks, key=lambda x: x["index"])]
        self._emit_reused("frame_interpolation_2x", paths[0].parent)
        return paths

    def _ensure_ffv1_rife_source(self, current: Path, duration: float) -> Path:
        assert self.state is not None
        stage = self.state.stage("frame_interpolation_2x")
        existing = stage.get("source_artifact")
        if fingerprint_matches(existing):
            return Path(existing["path"])
        probe = probe_media(self.toolchain.ffprobe, current)
        if first_stream(probe, "video").get("codec_name") == "ffv1":
            stage["source_artifact"] = fingerprint_file(current)
            stage["source_artifact_owned_by_interpolation"] = False
            self.state.save()
            return current
        attempt = self._attempt_dir("frame_interpolation_source")
        output = attempt / "rife-source.ffv1.mkv"
        run_logged_process(
            [
                str(self.toolchain.ffmpeg),
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(current),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-c:v",
                "ffv1",
                "-level",
                str(LADA_INTERMEDIATE_LEVEL),
                "-pix_fmt",
                FINAL_PIXEL_FORMAT,
                "-progress",
                "pipe:1",
                "-nostats",
                str(output),
            ],
            log_path=self.logs_dir / "frame-interpolation.log",
            progress=self._progress("frame_interpolation_2x"),
            duration_seconds=duration,
        )
        self._validate_intermediate(output, current)
        artifact = self.artifacts_dir / f"rife-source-{uuid.uuid4().hex}.ffv1.mkv"
        os.replace(output, artifact)
        stage["source_artifact"] = fingerprint_file(artifact)
        stage["source_artifact_owned_by_interpolation"] = True
        self.state.save()
        safe_remove_tree(attempt, self.job_dir)
        self.state.record_cleanup(attempt, "RIFE 输入已转为可精确分块的 FFV1")
        return artifact

    def _run_interpolation(
        self, *, current: Path, duration: float, source_rate: Fraction
    ) -> list[Path]:
        assert self.state is not None and self.emitter is not None
        stage_name = "frame_interpolation_2x"
        self.state.start_stage(
            stage_name,
            model=RIFE_MODEL,
            gpu_id=RIFE_GPU_ID,
            threads=RIFE_THREADS,
            chunk_source_frame_pairs=self.request.rife_chunk_frames,
        )
        self.emitter.emit("stage_started", job_id=self.job_id, stage=stage_name)
        try:
            rife_source = self._ensure_ffv1_rife_source(current, duration)
            source_probe = probe_media(self.toolchain.ffprobe, rife_source, count_frames=True)
            total_frames = decoded_frame_count(first_stream(source_probe, "video"))
            if total_frames < 2:
                raise PipelineError("RIFE 至少需要 2 个源帧")
            total_pairs = total_frames - 1
            total_chunks = math.ceil(total_pairs / self.request.rife_chunk_frames)
            existing_records = {
                int(item["index"]): item
                for item in self.state.stage(stage_name).get("chunks", [])
                if "index" in item
            }
            chunk_paths: list[Path] = []
            for index in range(total_chunks):
                record = existing_records.get(index)
                if record and fingerprint_matches(record.get("artifact")):
                    chunk_paths.append(Path(record["artifact"]["path"]))
                    self._progress(stage_name)(
                        index / total_chunks * 100,
                        f"复用已完成分块 {index + 1}/{total_chunks}",
                    )
                    continue
                pair_start = index * self.request.rife_chunk_frames
                pair_end = min(total_pairs, pair_start + self.request.rife_chunk_frames)
                pair_count = pair_end - pair_start
                input_frame_count = pair_count + 1
                rife_output_count = input_frame_count * 2 - 1
                skip_first = 1 if index else 0
                encoded_count = rife_output_count - skip_first
                artifact = self._run_rife_chunk(
                    rife_source=rife_source,
                    source_rate=source_rate,
                    index=index,
                    total_chunks=total_chunks,
                    pair_start=pair_start,
                    input_frame_count=input_frame_count,
                    rife_output_count=rife_output_count,
                    skip_first=skip_first,
                    encoded_count=encoded_count,
                )
                chunk_paths.append(artifact)
                chunk_record = {
                    "index": index,
                    "pair_start": pair_start,
                    "pair_end": pair_end,
                    "input_frames": input_frame_count,
                    "output_frames": encoded_count,
                    "artifact": fingerprint_file(artifact),
                }
                existing_records[index] = chunk_record
                self.state.stage(stage_name)["chunks"] = [
                    existing_records[key] for key in sorted(existing_records)
                ]
                self.state.stage(stage_name)["total_chunks"] = total_chunks
                self.state.save()
                self._progress(stage_name)(
                    (index + 1) / total_chunks * 100,
                    f"完成分块 {index + 1}/{total_chunks}",
                )
            expected_total = total_frames * 2 - 1
            actual_total = sum(
                int(item["output_frames"]) for item in existing_records.values()
            )
            if actual_total != expected_total:
                raise PipelineError(
                    f"RIFE 分块帧数不闭合: expected={expected_total} actual={actual_total}"
                )
            self.state.complete_stage(
                stage_name,
                source_frames=total_frames,
                expected_output_frames=expected_total,
                chunks=[existing_records[key] for key in sorted(existing_records)],
            )
            self.emitter.emit(
                "stage_completed",
                job_id=self.job_id,
                stage=stage_name,
                source_frames=total_frames,
                output_frames=expected_total,
                chunks=total_chunks,
            )
            owned_source = self.state.stage(stage_name).get(
                "source_artifact_owned_by_interpolation"
            )
            if self.request.cleanup_on_success and owned_source:
                self._cleanup_file(rife_source, "RIFE 输入已由全部无损插帧分块取代")
            return chunk_paths
        except Exception as exc:
            self.state.fail_stage(stage_name, str(exc))
            raise

    def _run_rife_chunk(
        self,
        *,
        rife_source: Path,
        source_rate: Fraction,
        index: int,
        total_chunks: int,
        pair_start: int,
        input_frame_count: int,
        rife_output_count: int,
        skip_first: int,
        encoded_count: int,
    ) -> Path:
        attempt = self._attempt_dir("frame_interpolation_2x")
        input_dir = attempt / "input"
        output_dir = attempt / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        start_seconds = Fraction(pair_start, 1) / source_rate
        start_text = f"{float(start_seconds):.9f}"
        doubled_rate = source_rate * 2
        rate_text = f"{doubled_rate.numerator}/{doubled_rate.denominator}"
        log_path = self.logs_dir / "frame-interpolation.log"
        run_logged_process(
            [
                str(self.toolchain.ffmpeg),
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-ss",
                start_text,
                "-i",
                str(rife_source),
                "-map",
                "0:v:0",
                "-frames:v",
                str(input_frame_count),
                "-fps_mode",
                "passthrough",
                "-start_number",
                "0",
                str(input_dir / "%08d.png"),
            ],
            log_path=log_path,
        )
        extracted = len(list(input_dir.glob("*.png")))
        if extracted != input_frame_count:
            raise PipelineError(
                f"RIFE 分块 {index + 1}/{total_chunks} 提取帧数错误: "
                f"expected={input_frame_count} actual={extracted}"
            )
        with TemporaryAsciiMappings() as mappings:
            ascii_tool = mappings.map_directory(self.toolchain.rife_dir)
            ascii_attempt = mappings.map_directory(attempt)
            run_logged_process(
                [
                    str(ascii_tool / self.toolchain.rife_exe.name),
                    "-i",
                    str(ascii_attempt / "input"),
                    "-o",
                    str(ascii_attempt / "output"),
                    "-n",
                    str(rife_output_count),
                    "-m",
                    RIFE_MODEL,
                    "-g",
                    str(RIFE_GPU_ID),
                    "-j",
                    RIFE_THREADS,
                    "-f",
                    "%08d.png",
                ],
                cwd=ascii_tool,
                log_path=log_path,
                progress=self._progress("frame_interpolation_2x"),
            )
        actual_rife = len(list(output_dir.glob("*.png")))
        if actual_rife != rife_output_count:
            raise PipelineError(
                f"RIFE 分块 {index + 1}/{total_chunks} 输出帧数错误: "
                f"expected={rife_output_count} actual={actual_rife}"
            )
        chunk_partial = attempt / "chunk.ffv1.mkv"
        run_logged_process(
            [
                str(self.toolchain.ffmpeg),
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-framerate",
                rate_text,
                "-start_number",
                str(skip_first),
                "-i",
                str(output_dir / "%08d.png"),
                "-frames:v",
                str(encoded_count),
                "-c:v",
                "ffv1",
                "-level",
                str(LADA_INTERMEDIATE_LEVEL),
                "-pix_fmt",
                FINAL_PIXEL_FORMAT,
                str(chunk_partial),
            ],
            log_path=log_path,
        )
        probe = probe_media(self.toolchain.ffprobe, chunk_partial, count_frames=True)
        actual_encoded = decoded_frame_count(first_stream(probe, "video"))
        if actual_encoded != encoded_count:
            raise PipelineError(
                f"RIFE 无损分块帧数错误: expected={encoded_count} actual={actual_encoded}"
            )
        chunks_dir = self.artifacts_dir / "rife-chunks"
        chunks_dir.mkdir(exist_ok=True)
        artifact = chunks_dir / f"chunk-{index:05d}-{uuid.uuid4().hex}.ffv1.mkv"
        os.replace(chunk_partial, artifact)
        safe_remove_tree(attempt, self.job_dir)
        assert self.state is not None
        self.state.record_cleanup(attempt, f"RIFE 分块 {index + 1} PNG 已转为 FFV1")
        return artifact

    def _run_final_output(
        self,
        *,
        current: Path,
        interpolation_chunks: list[Path] | None,
        source_rate: Fraction,
        duration: float,
    ) -> Path:
        assert self.state is not None and self.emitter is not None
        stage = (
            "final_encode_and_audio_mux"
            if self.request.options.has_visual_processing
            else "passthrough_remux"
        )
        reusable = self.state.reusable_artifact(stage)
        if reusable is not None:
            self._emit_reused(stage, reusable)
            return reusable
        self.state.start_stage(stage)
        self.emitter.emit("stage_started", job_id=self.job_id, stage=stage)
        attempt = self._attempt_dir(stage)
        output = attempt / "final-candidate.mkv"
        args = [str(self.toolchain.ffmpeg), "-v", "error", "-nostdin", "-n"]
        if not self.request.options.has_visual_processing:
            args.extend(
                [
                    "-i",
                    str(self.input_video),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                    "-sn",
                ]
            )
        elif interpolation_chunks:
            concat = attempt / "rife-chunks.ffconcat"
            lines = ["ffconcat version 1.0"]
            for chunk in interpolation_chunks:
                escaped = str(chunk.resolve()).replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            concat.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            args.extend(
                [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-i",
                    str(self.input_video),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a?",
                ]
            )
            if self.request.options.enable_quality_enhancement:
                args.extend(["-vf", QUALITY_FINAL_CAS_FILTER])
            args.extend(self._final_video_args())
            doubled_rate = source_rate * 2
            args.extend(
                ["-r", f"{doubled_rate.numerator}/{doubled_rate.denominator}"]
            )
            args.extend(["-c:a", "copy", "-sn"])
        else:
            if current == self.input_video:
                args.extend(
                    [
                        "-i",
                        str(current),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a?",
                    ]
                )
            else:
                args.extend(
                    [
                        "-i",
                        str(current),
                        "-i",
                        str(self.input_video),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a?",
                    ]
                )
            if self.request.options.enable_quality_enhancement:
                args.extend(["-vf", QUALITY_FINAL_CAS_FILTER])
            args.extend(self._final_video_args())
            args.extend(["-r", f"{source_rate.numerator}/{source_rate.denominator}"])
            args.extend(["-c:a", "copy", "-sn"])
        args.extend(["-map_chapters", "-1", "-progress", "pipe:1", "-nostats", str(output)])
        try:
            run_logged_process(
                args,
                log_path=self.logs_dir / "final-output.log",
                progress=self._progress(stage),
                duration_seconds=duration,
            )
            if not output.is_file():
                raise PipelineError("最终编码/封装没有生成候选 MKV")
            artifact = self.artifacts_dir / f"final-{uuid.uuid4().hex}.candidate.mkv"
            os.replace(output, artifact)
            fingerprint = fingerprint_file(artifact)
            self.state.complete_stage(stage, artifact=fingerprint)
            safe_remove_tree(attempt, self.job_dir)
            self.state.record_cleanup(attempt, "最终候选已移入任务恢复点")
            self.emitter.emit(
                "stage_completed",
                job_id=self.job_id,
                stage=stage,
                artifact=str(artifact),
                sha256=fingerprint["sha256"],
            )
            return artifact
        except Exception as exc:
            self.state.fail_stage(stage, str(exc))
            raise

    @staticmethod
    def _final_video_args() -> list[str]:
        return [
            "-c:v",
            FINAL_VIDEO_CODEC,
            "-preset",
            FINAL_PRESET,
            "-crf",
            str(FINAL_CRF),
            "-pix_fmt",
            FINAL_PIXEL_FORMAT,
        ]

    def _run_validation(
        self,
        *,
        final_candidate: Path,
        input_fingerprint: dict[str, Any],
        normalized_request: dict[str, Any],
    ) -> dict[str, Any]:
        assert self.state is not None and self.emitter is not None
        stage = "technical_validation"
        self.state.start_stage(stage, candidate=str(final_candidate))
        self.emitter.emit("stage_started", job_id=self.job_id, stage=stage)
        try:
            report = validate_output(
                ffmpeg=self.toolchain.ffmpeg,
                ffprobe=self.toolchain.ffprobe,
                source=self.input_video,
                output=final_candidate,
                options=self.request.options,
                input_fingerprint=input_fingerprint,
                request=normalized_request,
                log_path=self.logs_dir / "technical-validation.log",
                progress=self._progress(stage),
            )
            write_report(self.report_path, report)
            self.state.complete_stage(
                stage,
                report_path=str(self.report_path),
                report_status=report["status"],
                candidate=report["output"],
            )
            self.emitter.emit(
                "stage_completed",
                job_id=self.job_id,
                stage=stage,
                status=report["status"],
                report=str(self.report_path),
                visual_acceptance="not_performed",
            )
            return report
        except Exception as exc:
            self.state.fail_stage(stage, str(exc))
            raise

    def _cleanup_file(self, path: Path, reason: str) -> None:
        if not path.exists() or path == self.input_video or path == self.output_video:
            return
        safe_unlink(path, self.job_dir)
        assert self.state is not None
        self.state.record_cleanup(path, reason)

    def _cleanup_success_intermediates(
        self, interpolation_chunks: list[Path] | None
    ) -> None:
        assert self.state is not None
        for stage_name in (
            "demosaic",
            "quality_enhancement",
            "final_encode_and_audio_mux",
            "passthrough_remux",
        ):
            stage = self.state.data.get("stages", {}).get(stage_name, {})
            artifact = stage.get("artifact")
            if artifact:
                path = Path(artifact["path"])
                if path.exists():
                    self._cleanup_file(path, "任务成功且正式输出已通过验收")
        if interpolation_chunks:
            for chunk in interpolation_chunks:
                self._cleanup_file(chunk, "任务成功且正式输出已通过验收")
