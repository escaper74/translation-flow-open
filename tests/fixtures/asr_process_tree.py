"""Sleeping ASR/Whisper stand-ins: no model, media, network, or GPU."""
import os
from pathlib import Path
import subprocess
import sys
import time

if sys.argv[1] == "--child":
    work = Path(sys.argv[2])
    (work / "child.pid").write_text(str(os.getpid()), encoding="ascii")
    time.sleep(30)
else:
    work = Path(sys.argv[sys.argv.index("--work-dir") + 1])
    work.mkdir(parents=True, exist_ok=True)
    (work / "parent.pid").write_text(str(os.getpid()), encoding="ascii")
    child = subprocess.Popen(
        [sys.executable, __file__, "--child", str(work)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child.wait()
