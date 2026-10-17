"""winutil.py — small helpers for running shell / PowerShell on Windows.

All command execution goes through here so timeouts, encoding and the hidden
window flag are handled consistently. Runs async via asyncio subprocesses.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

# hide child console windows when spawned from the service
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


async def run(cmd: str, timeout: float = 60.0, shell: str = "cmd") -> tuple[int, str]:
    """Run a command line and capture combined output.

    shell = "cmd" | "powershell". Returns (exit_code, text). On timeout returns
    (124, "<timeout>").
    """
    if shell == "powershell":
        argv = ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", cmd]
    else:
        argv = ["cmd", "/c", cmd]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        return 127, f"not found: {exc}"

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "<timeout>"

    text = (out or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode if proc.returncode is not None else -1, text


async def powershell(script: str, timeout: float = 60.0) -> tuple[int, str]:
    return await run(script, timeout=timeout, shell="powershell")
