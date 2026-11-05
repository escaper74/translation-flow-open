"""Small standalone runtime; installed separately from the source checkout."""
import os
from pathlib import Path
from translation_flow.runtime import tool_root

MODEL = "upscayl-lite-4x"
VERSION = "upscayl-2.15.0-lite"
NOMOS_MODEL = "nomosuni-otf-medium"
MODELS = {
    MODEL: dict(label="Upscayl Lite", version=VERSION, executable="upscayl-bin.exe", weights=MODEL),
    NOMOS_MODEL: dict(label="NomosUni OTF Medium", version="nomosuni-otf-medium-ncnn-20240831",
                      executable="span-ncnn-vulkan.exe", weights="2xNomosUni_compact_otf_medium_fp16"),
}


def model_spec(model=MODEL):
    if not isinstance(model, str) or model not in MODELS:
        raise ValueError("请选择 Lite 或 NomosUni OTF Medium。")
    return MODELS[model]


def runtime_root(tools=None):
    return Path(os.environ.get("TRANSLATION_FLOW_SR_ROOT") or
                tool_root(tools) / "super-resolution").resolve()


def model_root(root=None, model=MODEL):
    model_spec(model)
    root = Path(root or runtime_root())
    return root / NOMOS_MODEL if model == NOMOS_MODEL else root


def required_files(root=None, model=MODEL):
    root, spec = model_root(root, model), model_spec(model)
    return [root / spec["executable"], root / "vcomp140.dll",
            root / f"models/{spec['weights']}.bin", root / f"models/{spec['weights']}.param"]


def check_runtime(root=None, model=MODEL):
    missing = [p.name for p in required_files(root, model) if not p.is_file() or not p.stat().st_size]
    if missing:
        raise FileNotFoundError("缺少 " + model_spec(model)["label"] + " 组件：" + ", ".join(missing) +
                                "；请运行项目根目录中的‘安装轻量超分.cmd’。")
