"""net_pref.py — remember the user's chosen internet path.

Survives status.json overwrites. Warm-checks and keep-alives must not steal
away from this preference (especially SMS ↔ DSL Wi‑Fi). All three paths are
their own box/WAN now, so an eth choice persists across reboot like the
others; with no preference stored the default is Wi‑Fi (DSL).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from config import HERE

log = logging.getLogger("net_pref")
_lock = threading.Lock()
PATH = os.path.join(HERE, "preferred_net.json")


def get() -> str:
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        src = str(data.get("source") or "").lower().strip()
        if src in ("wifi", "eth", "sms"):
            return src
    except Exception:                                # noqa: BLE001
        pass
    return ""


def set_source(source: str) -> None:
    source = (source or "").lower().strip()
    if source not in ("wifi", "eth", "sms"):
        return
    with _lock:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"source": source}, fh)
        os.replace(tmp, PATH)
    log.info("preferred net set to %s", source)
