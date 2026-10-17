"""net_request.py — UI → elevated core switch requests.

display_app runs in the user session and cannot Set-NetIPInterface
(Access Denied). It drops a request file; LocalServerCore (SYSTEM) applies
the switch and writes the result.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from config import HERE

LOG_DIR = os.path.join(HERE, "logs")
REQUEST_PATH = os.path.join(LOG_DIR, "net_request.json")
RESULT_PATH = os.path.join(LOG_DIR, "net_result.json")


def submit(source: str) -> str:
    """Queue a switch request. Returns request id."""
    source = (source or "").lower().strip()
    rid = uuid.uuid4().hex[:12]
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = {
        "id": rid,
        "source": source,
        "at": time.time(),
    }
    tmp = REQUEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, REQUEST_PATH)
    # clear stale result for this id
    try:
        if os.path.isfile(RESULT_PATH):
            os.remove(RESULT_PATH)
    except OSError:
        pass
    return rid


def take() -> dict | None:
    """Core: claim one pending request (idempotent)."""
    if not os.path.isfile(REQUEST_PATH):
        return None
    try:
        with open(REQUEST_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:                                # noqa: BLE001
        try:
            os.remove(REQUEST_PATH)
        except OSError:
            pass
        return None
    try:
        os.remove(REQUEST_PATH)
    except OSError:
        pass
    if not isinstance(data, dict) or not data.get("source"):
        return None
    return data


def write_result(req_id: str, source: str, res: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = {
        "id": req_id,
        "source": source,
        "ok": bool(res.get("ok")),
        "output": res.get("output") or "",
        "error": res.get("error") or "",
        "ip": res.get("ip") or "",
        "net": res.get("net") or source,
        "at": time.time(),
    }
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, RESULT_PATH)


def wait_result(req_id: str, timeout: float = 45.0) -> dict:
    """UI: poll for result matching req_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(RESULT_PATH):
            try:
                with open(RESULT_PATH, encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("id") == req_id:
                    return data
            except Exception:                        # noqa: BLE001
                pass
        time.sleep(0.35)
    return {"ok": False, "error": "timeout waiting for core to switch network",
            "id": req_id}
