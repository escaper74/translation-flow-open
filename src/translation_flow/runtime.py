"""Portable local runtime locations shared by the backend entry points."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOL_ROOT = PROJECT_ROOT / "workflow" / "tools"


def tool_root(value: str | Path | None = None, *, project_root: Path | None = None) -> Path:
    return Path(value or os.environ.get("TRANSLATION_FLOW_TOOL_ROOT")
                or (project_root or PROJECT_ROOT) / "workflow" / "tools").resolve()


def jasna_path(value: str | Path | None = None, *, tools: Path) -> Path:
    return Path(value or os.environ.get("TRANSLATION_FLOW_JASNA") or tools / "jasna" / "jasna.exe").resolve()


def acceptance_report(value: str | Path | None = None, *, tools: Path) -> Path:
    return Path(value or os.environ.get("TRANSLATION_FLOW_ACCEPTANCE_REPORT")
                or tools / "acceptance" / "workflow-report.json").resolve()
