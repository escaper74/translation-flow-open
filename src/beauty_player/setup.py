"""Explicit one-time download of small, pinned public runtime assets, never media."""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

VERSION = "0.10.21"
FILES = ["vision_bundle.mjs", "wasm/vision_wasm_internal.js", "wasm/vision_wasm_internal.wasm",
         "wasm/vision_wasm_nosimd_internal.js", "wasm/vision_wasm_nosimd_internal.wasm"]
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
DEFAULT = Path(__file__).resolve().parents[2] / "workflow/tools/beauty-player"

def get(url):
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read(50 * 1024 * 1024 + 1)

def install(destination: Path):
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    meta = json.loads(get(f"https://registry.npmjs.org/@mediapipe/tasks-vision/{VERSION}"))
    package = get(meta["dist"]["tarball"])
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode()
    if integrity != meta["dist"]["integrity"]:
        raise ValueError("MediaPipe package integrity mismatch")
    assets = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        for name in FILES:
            assets[name] = archive.extractfile("package/" + name).read()
    assets["face_landmarker.task"] = get(MODEL_URL)
    assets["MEDIAPIPE-LICENSE.txt"] = get("https://raw.githubusercontent.com/google-ai-edge/mediapipe/v0.10.21/LICENSE")
    for name, data in assets.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing different runtime asset: {target}")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
    manifest = {"version": VERSION, "npm_integrity": integrity, "model_url": MODEL_URL,
                "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in assets.items()}}
    (destination / "runtime.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ready", "directory": str(destination),
                      "bytes": sum(len(data) for data in assets.values())}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=DEFAULT)
    install(parser.parse_args().destination)
