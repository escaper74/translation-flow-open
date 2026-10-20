#!/usr/bin/env python3
"""Download one pinned Hugging Face snapshot into an explicit local directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--local-dir", required=True, type=Path)
    args = parser.parse_args()
    args.local_dir.mkdir(parents=True, exist_ok=True)
    resolved = snapshot_download(
        repo_id=args.repo_id,
        revision=args.revision,
        local_dir=str(args.local_dir),
    )
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
