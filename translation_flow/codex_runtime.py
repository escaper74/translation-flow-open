"""Locate the installed official CLI without requiring a terminal-specific PATH."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import urllib.request


def resolve_codex_executable() -> str | None:
    executable = shutil.which("codex")
    if executable:
        return executable
    # The Windows desktop app bundles its CLI in a versioned directory which
    # is not normally added to Explorer's PATH. Do not pin a release hash.
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path for path in root.glob("*/codex.exe") if path.is_file()]
        return str(max(candidates, key=lambda path: path.stat().st_mtime_ns)) if candidates else None
    except OSError:
        return None


def inherit_windows_proxy(env: dict[str, str]) -> dict[str, str]:
    """Follow the current user's existing proxy, without changing OS settings."""
    result = dict(env)
    if any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for key in result):
        return result
    registry_proxies = getattr(urllib.request, 'getproxies_registry', None)
    if registry_proxies is None:
        return result
    try:
        proxies = registry_proxies()
    except OSError:
        return result
    for protocol in ('http', 'https'):
        if proxies.get(protocol):
            result[protocol.upper() + '_PROXY'] = proxies[protocol]
    if proxies.get('no') and not any(key.lower() == 'no_proxy' for key in result):
        result['NO_PROXY'] = proxies['no']
    return result
