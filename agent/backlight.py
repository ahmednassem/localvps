"""backlight.py — screen backlight brightness (not display power-off).

Uses WMI WmiMonitorBrightnessMethods. Saves the current level before dimming so
Space×3 or an error wake can restore it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("backlight")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_STATE_NAME = "backlight_state.json"


def _ps(script: str, timeout: float = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        if r.stderr:
            out = (out + "\n" + r.stderr.strip()).strip()
        return r.returncode, out
    except Exception as exc:                        # noqa: BLE001
        return 1, str(exc)


def _state_path(cfg) -> str:
    return os.path.join(cfg.log_dir, _STATE_NAME)


def get_brightness() -> int | None:
    code, out = _ps(
        "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness "
        "| Select-Object -First 1).CurrentBrightness"
    )
    if code != 0 or not out:
        return None
    try:
        return int(float(out.splitlines()[-1].strip()))
    except ValueError:
        return None


def set_brightness(pct: int) -> bool:
    pct = max(0, min(100, int(pct)))
    code, out = _ps(
        f"$m = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods; "
        f"if ($null -eq $m) {{ exit 2 }}; "
        f"$m.WmiSetBrightness(1, {pct}); Write-Output 'ok'"
    )
    if code != 0:
        log.warning("set_brightness(%s) failed: %s", pct, out)
        return False
    return True


def save_level(cfg, level: int | None = None) -> None:
    if level is None:
        level = get_brightness()
    if level is None:
        return
    os.makedirs(cfg.log_dir, exist_ok=True)
    with open(_state_path(cfg), "w", encoding="utf-8") as fh:
        json.dump({"saved": level}, fh)


def restore_level(cfg, default: int = 70) -> bool:
    saved = default
    try:
        with open(_state_path(cfg), encoding="utf-8") as fh:
            saved = int(json.load(fh).get("saved", default))
    except Exception:                               # noqa: BLE001
        pass
    return set_brightness(saved)


def dim(cfg, level: int = 0) -> bool:
    """Turn backlight down (default 0%). Saves previous level first."""
    cur = get_brightness()
    if cur is not None and cur > level:
        save_level(cfg, cur)
    return set_brightness(level)
