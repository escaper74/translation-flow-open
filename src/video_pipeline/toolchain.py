from __future__ import annotations

import ctypes
import json
import os
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    LADA_DETECTION_MODEL_SHA256,
    LADA_RESTORATION_MODEL_SHA256,
    LADA_VERSION,
    RIFE_RELEASE,
)
from .state import canonical_hash, sha256_file


def _first_line(value: str) -> str:
    return value.replace("\r", "").split("\n", 1)[0].strip()


def _run_capture(
    args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=60,
    )


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {"path": str(root.resolve()), "files": files, "tree_sha256": canonical_hash(files)}


def _inspect_windows_gpu_engines() -> list[dict[str, Any]]:
    script = r"""
$ErrorActionPreference = 'Stop'
$sets = Get-Counter '\GPU Engine(*)\Utilization Percentage' -SampleInterval 1 -MaxSamples 2
$items = foreach ($set in $sets) {
    $active = @($set.CounterSamples | Where-Object {
        $_.InstanceName -match 'engtype_(compute|cuda|videoencode)' -and $_.CookedValue -ge 0.5
    })
    [pscustomobject]@{
        timestamp = $set.Timestamp.ToUniversalTime().ToString('o')
        blocking_utilization_percent = [double](($active | Measure-Object -Property CookedValue -Sum).Sum)
        engines = @($active | Select-Object InstanceName, CookedValue)
    }
}
$items | ConvertTo-Json -Depth 5 -Compress
"""
    result = _run_capture(
        ["powershell.exe", "-NoProfile", "-Command", script]
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "无法读取 Windows GPU 引擎占用，拒绝在未知状态下开始 GPU 任务"
        )
    parsed = json.loads(result.stdout)
    return parsed if isinstance(parsed, list) else [parsed]


def _inspect_nvidia_gpu(*, enforce_idle: bool) -> dict[str, Any]:
    gpu_samples: list[dict[str, Any]] = []
    sample_count = 3 if enforce_idle else 1
    for index in range(sample_count):
        try:
            gpu = _run_capture(
                [
                    "nvidia-smi.exe",
                    "--query-gpu=name,driver_version,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ]
            )
        except FileNotFoundError:
            if enforce_idle:
                raise RuntimeError("找不到 nvidia-smi，拒绝开始 GPU 任务") from None
            return {}
        if gpu.returncode != 0:
            if enforce_idle:
                raise RuntimeError("nvidia-smi GPU 检查失败，拒绝开始 GPU 任务")
            return {}
        first_gpu = gpu.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in first_gpu.split(",")]
        if len(parts) < 8:
            raise RuntimeError(f"无法解析 nvidia-smi 输出: {first_gpu}")
        gpu_samples.append(
            {
                "name": parts[0],
                "driver_version": parts[1],
                "memory_total_mib": int(parts[2]),
                "memory_used_mib": int(parts[3]),
                "memory_free_mib": int(parts[4]),
                "utilization_gpu_percent": int(parts[5]),
                "utilization_memory_percent": int(parts[6]),
                "temperature_c": int(parts[7]),
            }
        )
        if index + 1 < sample_count:
            time.sleep(1)
    result: dict[str, Any] = {
        "nvidia_gpu_identity": {
            "name": gpu_samples[0]["name"],
            "driver_version": gpu_samples[0]["driver_version"],
            "memory_total_mib": gpu_samples[0]["memory_total_mib"],
        },
        "nvidia_gpu_samples": gpu_samples,
    }
    if enforce_idle:
        sorted_utilization = sorted(
            sample["utilization_gpu_percent"] for sample in gpu_samples
        )
        median_utilization = sorted_utilization[len(sorted_utilization) // 2]
        minimum_free_mib = min(sample["memory_free_mib"] for sample in gpu_samples)
        if os.name == "nt":
            engine_samples = _inspect_windows_gpu_engines()
            result["nvidia_gpu_engine_samples"] = engine_samples
            blocking_utilization = max(
                float(sample["blocking_utilization_percent"])
                for sample in engine_samples
            )
        else:
            engine_samples = []
            blocking_utilization = float(median_utilization)
        if blocking_utilization > 10 or minimum_free_mib < 6144:
            raise RuntimeError(
                "GPU 当前忙碌，拒绝与其他计算任务并发："
                f"计算/编码引擎最高占用 {blocking_utilization:.1f}%，"
                f"nvidia-smi 总利用率中位数 {median_utilization}%，"
                f"最低空闲显存 {minimum_free_mib} MiB；"
                "请等待现有 Whisper/TTS/画面任务结束后用相同参数恢复"
            )
    return result


@dataclass(frozen=True)
class Toolchain:
    root: Path
    ffmpeg: Path
    ffprobe: Path
    lada_dir: Path
    lada_cli: Path
    lada_weights: Path
    lada_restoration_model: Path
    lada_detection_model: Path
    rife_dir: Path
    rife_exe: Path
    rife_model_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> "Toolchain":
        root = root.expanduser().resolve()
        ffmpeg_dir = root / "ffmpeg" / "ffmpeg-8.1.1-essentials_build" / "bin"
        lada_dir = root / "media-lab" / "lada" / "app-v0.11.0" / "lada"
        weights = lada_dir / "_internal" / "model_weights"
        rife_dir = (
            root
            / "media-lab"
            / "ncnn"
            / "rife-ncnn-vulkan-20221029"
            / "rife-ncnn-vulkan-20221029-windows"
        )
        return cls(
            root=root,
            ffmpeg=ffmpeg_dir / "ffmpeg.exe",
            ffprobe=ffmpeg_dir / "ffprobe.exe",
            lada_dir=lada_dir,
            lada_cli=lada_dir / "lada-cli.exe",
            lada_weights=weights,
            lada_restoration_model=(
                weights / "lada_mosaic_restoration_model_generic_v1.2.pth"
            ),
            lada_detection_model=weights / "lada_mosaic_detection_model_v4_fast.pt",
            rife_dir=rife_dir,
            rife_exe=rife_dir / "rife-ncnn-vulkan.exe",
            rife_model_dir=rife_dir / "rife-v4.6",
        )

    def validate(self, *, need_lada: bool, need_rife: bool) -> None:
        required = [self.ffmpeg, self.ffprobe]
        if need_lada:
            required.extend(
                [
                    self.lada_cli,
                    self.lada_restoration_model,
                    self.lada_detection_model,
                ]
            )
        if need_rife:
            required.extend([self.rife_exe, self.rife_model_dir])
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError("缺少本机工具或模型:\n" + "\n".join(missing))
        if need_lada:
            actual_restoration = sha256_file(self.lada_restoration_model)
            actual_detection = sha256_file(self.lada_detection_model)
            if actual_restoration != LADA_RESTORATION_MODEL_SHA256:
                raise RuntimeError("Lada basicvsrpp-v1.2 模型指纹与 07 基线不符")
            if actual_detection != LADA_DETECTION_MODEL_SHA256:
                raise RuntimeError("Lada v4-fast 检测模型指纹与 07 基线不符")

    def lada_environment(self, runtime_dir: Path) -> dict[str, str]:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        locations = {
            "YOLO_CONFIG_DIR": runtime_dir / "ultralytics",
            "MPLCONFIGDIR": runtime_dir / "matplotlib",
            "TORCH_HOME": runtime_dir / "torch",
            "XDG_CACHE_HOME": runtime_dir / "xdg-cache",
            "XDG_CONFIG_HOME": runtime_dir / "xdg-config",
        }
        env = os.environ.copy()
        for name, path in locations.items():
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        # 0.11.0 的帮助文本曾出现过一次拼写错误；两个名字都限定到只读模型目录。
        env["LADA_MODEL_WEIGHTS_DIR"] = str(self.lada_weights)
        env["LADA_MODEL_WEIGETS_DIR"] = str(self.lada_weights)
        return env

    def inspect_versions(
        self, *, runtime_dir: Path, need_lada: bool, need_rife: bool
    ) -> dict[str, Any]:
        ffmpeg_result = _run_capture([str(self.ffmpeg), "-version"])
        ffprobe_result = _run_capture([str(self.ffprobe), "-version"])
        if ffmpeg_result.returncode != 0 or ffprobe_result.returncode != 0:
            raise RuntimeError("FFmpeg/FFprobe 版本检查失败")
        result: dict[str, Any] = {
            "tool_root": str(self.root),
            "ffmpeg": _first_line(ffmpeg_result.stdout),
            "ffprobe": _first_line(ffprobe_result.stdout),
            "ffmpeg_sha256": sha256_file(self.ffmpeg),
            "ffprobe_sha256": sha256_file(self.ffprobe),
        }
        # 先用轻量 nvidia-smi 判断是否忙碌，避免连 Lada 的设备枚举都与现有任务争抢 CUDA。
        result.update(_inspect_nvidia_gpu(enforce_idle=need_lada or need_rife))
        if need_lada:
            lada_result = _run_capture(
                [str(self.lada_cli), "--version"],
                cwd=runtime_dir,
                env=self.lada_environment(runtime_dir),
            )
            if lada_result.returncode != 0 or LADA_VERSION not in lada_result.stdout:
                raise RuntimeError(
                    "Lada 版本检查失败: "
                    + (lada_result.stdout + lada_result.stderr).strip()[-500:]
                )
            devices = _run_capture(
                [str(self.lada_cli), "--list-devices"],
                cwd=runtime_dir,
                env=self.lada_environment(runtime_dir),
            )
            if devices.returncode != 0 or "cuda:0" not in devices.stdout:
                raise RuntimeError("Lada 未检测到 cuda:0，拒绝开始长任务")
            result["lada"] = _first_line(lada_result.stdout)
            result["lada_devices"] = devices.stdout.strip()
            result["lada_cli_sha256"] = sha256_file(self.lada_cli)
            result["lada_restoration_model_sha256"] = sha256_file(
                self.lada_restoration_model
            )
            result["lada_detection_model_sha256"] = sha256_file(
                self.lada_detection_model
            )
        if need_rife:
            result["rife_release"] = RIFE_RELEASE
            result["rife_exe_sha256"] = sha256_file(self.rife_exe)
            result["rife_model"] = _tree_fingerprint(self.rife_model_dir)
        return result


def is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def get_short_path(path: Path) -> Path | None:
    if os.name != "nt" or not path.exists():
        return None
    kernel32 = ctypes.windll.kernel32
    needed = kernel32.GetShortPathNameW(str(path), None, 0)
    if not needed:
        return None
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if not written:
        return None
    result = Path(buffer.value)
    return result if is_ascii_path(result) else None


class TemporaryAsciiMappings:
    """仅在短路径不可用时，为旧 NCNN 临时建立 subst ASCII 路径。"""

    def __init__(self) -> None:
        self._mappings: list[tuple[str, Path]] = []

    def map_directory(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if is_ascii_path(resolved):
            return resolved
        short = get_short_path(resolved)
        if short is not None:
            return short
        if os.name != "nt":
            raise RuntimeError(f"工具要求 ASCII 路径，但无法建立 Windows 路径映射: {resolved}")
        drive = self._allocate_drive()
        result = _run_capture(["subst.exe", drive, str(resolved)])
        if result.returncode != 0:
            raise RuntimeError(f"无法为旧 NCNN 建立临时 ASCII 路径: {resolved}")
        self._mappings.append((drive, resolved))
        return Path(f"{drive}\\")

    def _allocate_drive(self) -> str:
        used = {f"{letter}:" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()}
        used.update(drive for drive, _ in self._mappings)
        for letter in reversed(string.ascii_uppercase[3:]):
            drive = f"{letter}:"
            if drive not in used:
                return drive
        raise RuntimeError("没有可用盘符用于临时 ASCII 路径映射")

    def close(self) -> None:
        failures: list[str] = []
        for drive, expected in reversed(self._mappings):
            mapped_root = Path(f"{drive}\\")
            try:
                identity_matches = mapped_root.exists() and os.path.samefile(
                    mapped_root, expected
                )
            except OSError:
                identity_matches = False
            if not identity_matches:
                failures.append(f"{drive} 的映射身份无法确认")
                continue
            removed = _run_capture(["subst.exe", drive, "/D"])
            if removed.returncode != 0:
                failures.append(f"{drive} 解除失败")
        self._mappings.clear()
        if failures:
            raise RuntimeError("；".join(failures))

    def __enter__(self) -> "TemporaryAsciiMappings":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False
