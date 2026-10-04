#!/usr/bin/env python3
"""Legacy command path; all translation behavior lives in translation_flow.translation."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from translation_flow.translation.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
