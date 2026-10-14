"""Best-effort desktop notices; never expose a title, subtitle, path or credential."""
import os
from pathlib import Path
import subprocess


def notify_terminal(status: str) -> bool:
    if os.name != "nt" or status not in {"completed", "failed", "interrupted"}:
        return False
    if os.environ.get("TRANSLATION_FLOW_NOTIFICATIONS", "1") == "0":
        return False
    script = Path(__file__).resolve().parents[1] / "desktop/Show-TranslationFlowNotification.ps1"
    env = dict(os.environ)
    env.pop("SUBTITLE_API_KEY", None)
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File", str(script), "-Status", status],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False  # Windows notification failure must not fail a completed movie.
