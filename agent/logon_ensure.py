"""logon_ensure.py — ensure LocalServerCore is running (scheduled at user logon)."""
from __future__ import annotations

import subprocess
import sys


def _service_state() -> str:
    r = subprocess.run(
        ["sc", "query", "LocalServerCore"],
        capture_output=True, text=True, timeout=15,
        creationflags=0x08000000,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return "unknown"


def main() -> int:
    state = _service_state()
    if state == "running":
        print("LocalServerCore already running")
        return 0
    print(f"LocalServerCore is {state} — starting…")
    r = subprocess.run(
        ["net", "start", "LocalServerCore"],
        capture_output=True, text=True, timeout=60,
        creationflags=0x08000000,
    )
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(out or f"exit {r.returncode}")
    return 0 if r.returncode == 0 else r.returncode


if __name__ == "__main__":
    sys.exit(main())
