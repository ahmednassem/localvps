"""status_store.py — local status file for the on-laptop GUI.

The core writes telemetry + connection state here every cycle. The display app
(user session) reads it. Alerts trigger the GUI to turn the backlight on and show
the error fullscreen.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

_lock = threading.Lock()


def path_for(cfg) -> str:
    return os.path.join(cfg.log_dir, "status.json")


def read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                               # noqa: BLE001
        return {}


def write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data["updated_at"] = time.time()
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)


def update(path: str, **fields) -> dict:
    data = read(path)
    data.update(fields)
    write(path, data)
    return data


def set_alert(path: str, message: str, level: str = "critical") -> None:
    update(path, alert={"level": level, "message": message, "at": time.time()})


def clear_alert(path: str) -> None:
    data = read(path)
    data.pop("alert", None)
    write(path, data)


def merge_telemetry(path: str, tele: dict, *, connection: dict,
                     host: str) -> None:
    data = {
        "host": host,
        "connection": connection,
        "telemetry": tele,
        "battery_pct": tele.get("battery"),
        "charging": tele.get("charging"),
        "plugged": tele.get("plugged"),
        "net": tele.get("net"),
        "ip": tele.get("ip"),
        "temp": tele.get("temp"),
        "cpu": tele.get("cpu"),
        "ram": tele.get("ram"),
    }
    prev = read(path)
    if "alert" in prev:
        data["alert"] = prev["alert"]
    write(path, data)
