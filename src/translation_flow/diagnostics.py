"""Read-only, offline environment inventory; does not authenticate or run tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .asr import ToolPaths
from .runtime import tool_root, jasna_path, acceptance_report
from .translation.api_providers import API_PROVIDERS
from .codex_runtime import resolve_codex_executable


def inspect_environment(*, mode="video", provider="codex", tools=None, jasna=None, gate=None,
                        asr_a_model="large-v3-turbo"):
    root = tool_root(tools)
    paths = {"python": Path(sys.executable)}
    optional = {}
    if mode in ("video", "full"):
        chain = ToolPaths.from_root(root, root / "asr-lab", a_model_id=asr_a_model)
        paths.update({name: getattr(chain, name) for name in (
            "faster_python", "ffmpeg", "ffprobe", "whisper", "whisper_model", "faster_model",
            "transcribe_a", "clean_srt", "validate_quality", "transcribe_c", "validate_structure")})
        optional.update(vad_cli=chain.vad_cli, vad_model=chain.vad_model)
    if mode in ("repackage", "picture"):
        chain = ToolPaths.from_root(root, root / "asr-lab")
        paths.update(ffmpeg=chain.ffmpeg, ffprobe=chain.ffprobe)
    if provider == "codex" and mode not in ("repackage", "picture"):
        paths["codex_cli"] = resolve_codex_executable()
    if mode in ("full", "picture"):
        paths.update(jasna=jasna_path(jasna, tools=root), acceptance_report=acceptance_report(gate, tools=root))
    checks = [{"name": name, "path": str(path) if path else None,
               "present": bool(path and (Path(path).is_dir() if name == "faster_model" else Path(path).is_file()))}
              for name, path in paths.items()]
    optional_checks = [{"name": name, "path": str(path), "present": path.is_file()}
                       for name, path in optional.items()]
    warnings = []
    if any(not item["present"] for item in optional_checks):
        warnings.append("开头局部校时组件缺失；正式任务会警告并保留原识别时间，不阻断翻译。")
    return {"status": "paths_available" if all(item["present"] for item in checks) else "missing_paths",
            "mode": mode, "provider": provider, "tool_root": str(root), "checks": checks,
            "asr_a_model": asr_a_model if mode in ("video", "full") else None,
            "optional_checks": optional_checks, "warnings": warnings,
            "scope": "只检查本地路径；未验证登录、API 连通、GPU、模型完整性或真实媒体处理。"}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="本地后端环境清单（不登录、不外发、不启动 GPU 或媒体工具）")
    parser.add_argument("--mode", choices=("srt", "video", "full", "picture", "repackage"), default="video")
    parser.add_argument("--provider", choices=(*API_PROVIDERS, "codex"), default="codex")
    parser.add_argument("--tool-root", type=Path)
    parser.add_argument("--asr-a-model", choices=("large-v3", "large-v3-turbo"),
                        default="large-v3-turbo", help="与桌面新任务默认一致；旧请求通常使用 large-v3。")
    parser.add_argument("--jasna", type=Path)
    parser.add_argument("--acceptance-gate-report", type=Path)
    args = parser.parse_args(argv)
    result = inspect_environment(mode=args.mode, provider=args.provider, tools=args.tool_root,
                                 jasna=args.jasna, gate=args.acceptance_gate_report,
                                 asr_a_model=args.asr_a_model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "paths_available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
