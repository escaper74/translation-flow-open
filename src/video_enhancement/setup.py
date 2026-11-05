"""Explicit small download, never launched implicitly by a media task."""
import argparse
import hashlib
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile
from .runtime import MODEL, NOMOS_MODEL, model_root, runtime_root

REV = "4f39acfc6f88260d105920a64deff8431d5e1544"
BASE = f"https://raw.githubusercontent.com/upscayl/upscayl/{REV}/"
# Git blob IDs verified against the upstream tree. No installers or system changes.
FILES = {
    "resources/win/bin/upscayl-bin.exe": ("upscayl-bin.exe", "67fc5d21a5d94c521dc3e46f17e002fe829e2b9e"),
    "resources/win/bin/vcomp140.dll": ("vcomp140.dll", "eadc83fb65dc18ec5bfe40cc714ea36fa480b092"),
    "resources/win/bin/vcomp140d.dll": ("vcomp140d.dll", "cc9a4947420e8956cab6042d15fc1711bbe287e3"),
    "resources/models/upscayl-lite-4x.bin": ("models/upscayl-lite-4x.bin", "e0a8b6ca061931284832a67cdd4112d94a7c98a0"),
    "resources/models/upscayl-lite-4x.param": ("models/upscayl-lite-4x.param", "221234ab0dc2641a11038f3682669ded6ba9e6d6"),
    "LICENSE": ("LICENSE-upscayl.txt", "0ad25db4bd1d86c452db3f9602ccdbe172438f52"),
}

NOMOS_RELEASE = "https://github.com/Phhofm/models/releases/download/2xNomosUni_compact_otf_medium/"
NCNN_PACKAGE = "span-ncnn-vulkan-20240831-055257-windows"
NCNN_URL = f"https://github.com/TNTwise/SPAN-ncnn-vulkan/releases/download/20240831-055257/{NCNN_PACKAGE}.zip"
# Project pins of the original release assets tested locally. These are not
# upstream-published digests. Keep the author's graph and weights unchanged.
NCNN_SHA256 = "ce72105410046e78fccd5a04498427538b0a20d8d30a1bc0f9f476bb9c8bfb6f"
NCNN_FILES = {
    "span-ncnn-vulkan.exe": "2af4620f0cb8024bb050308746a4f493d787704fe4e9ced7e94a78c03411e80f",
    "vcomp140.dll": "48b2cffa42a5064ba299eff1733753f4fa042d9d452c49c11d4fad27360f78ae",
    "vcomp140d.dll": "4d3af6635b382daf542f27e31681a90f30d09c137d16691c49eb4d6d9fb03b6a",
    "LICENSE": "392150e5ad83609ea48366e700c378fb8e97bf2eec5039bc35685039080f7a85",
}
NOMOS_FILES = {
    "2xNomosUni_compact_otf_medium_fp16.bin": "a900f056fc648d96bf9af21f5ab014043049f03d129849c5762a9a60e87848f0",
    "2xNomosUni_compact_otf_medium_fp16.param": "c9b75cf63a91cc29d4f374924d450705d11194eee948b4d846e1ed66d4c844d6",
}


def checked_existing(path, expected):
    if not path.exists():
        return False
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"已有组件与固定版本不同，保留原文件：{path.name}")
    return True


def save_checked(path, data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"组件内容校验失败：{path.name}")
    if checked_existing(path, expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
    print(f"已保存 {path.name} ({len(data)} bytes)")


def download(url):
    with urlopen(url, timeout=60) as response:
        return response.read()


def install_nomos(root, *, archive=None, models=None):
    """Install from pinned upstream assets, or reuse an existing local download."""
    root = model_root(root, NOMOS_MODEL)
    if not all(checked_existing(root / name, digest) for name, digest in NCNN_FILES.items()):
        data = Path(archive).read_bytes() if archive else download(NCNN_URL)
        if hashlib.sha256(data).hexdigest() != NCNN_SHA256:
            raise ValueError("NCNN 压缩包与固定版本不同。")
        with ZipFile(BytesIO(data)) as package:
            # Read only these fixed members; do not extract arbitrary ZIP paths.
            for name, digest in NCNN_FILES.items():
                save_checked(root / name, package.read(f"{NCNN_PACKAGE}/{name}"), digest)
    for name, digest in NOMOS_FILES.items():
        target = root / "models" / name
        if checked_existing(target, digest):
            continue
        data = (Path(models) / name).read_bytes() if models else download(NOMOS_RELEASE + name)
        save_checked(target, data, digest)
    notice = root / "THIRD_PARTY.txt"
    if not notice.exists():
        with notice.open("xb") as handle:
            handle.write(Path(__file__).with_name("THIRD_PARTY.txt").read_bytes())
    print("NomosUni OTF Medium 组件已就绪；未运行 GPU 推理。")


def blob_id(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def install(root):
    root.mkdir(parents=True, exist_ok=True)
    for source, (name, expected) in FILES.items():
        target = root / name
        if target.is_file() and expected and blob_id(target.read_bytes()) == expected:
            continue
        if target.exists():
            raise ValueError(f"已有组件与固定版本不同，保留原文件：{name}")
        with urlopen(BASE + source, timeout=60) as response:
            data = response.read()
        if expected and blob_id(data) != expected:
            raise ValueError(f"下载内容校验失败：{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        print(f"已保存 {name} ({len(data)} bytes)")
    print("轻量超分组件已就绪；未运行 GPU 推理。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=runtime_root())
    parser.add_argument("--model", choices=(MODEL, NOMOS_MODEL, "all"), default="all")
    parser.add_argument("--runtime-archive", type=Path, help="复用已有的 NCNN ZIP")
    parser.add_argument("--model-directory", type=Path, help="复用已有的 NomosUni 原版权重")
    args = parser.parse_args()
    if args.model in (MODEL, "all"):
        install(args.destination.resolve())
    if args.model in (NOMOS_MODEL, "all"):
        install_nomos(args.destination.resolve(), archive=args.runtime_archive, models=args.model_directory)
