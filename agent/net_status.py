"""Patch logs/status.json immediately after a network switch (UI + bot)."""
from __future__ import annotations

import asyncio
import logging
import time

import netctl
import status_store
from config import Config

log = logging.getLogger("net_status")


async def apply_after_switch(
    cfg: Config,
    source: str,
    res: dict,
    *,
    settle_s: float = 1.0,
) -> dict:
    """Refresh live route, enrich *res*, and write status.json for the display."""
    await asyncio.sleep(settle_s)
    src, ip = await netctl.active_source(cfg)
    live = (src or source).split(":")[0].lower()
    net_label = source if res.get("ok") else live
    out = dict(res)
    out["ip"] = ip
    out["net"] = net_label
    out["active"] = src

    try:
        path = status_store.path_for(cfg)
        data = status_store.read(path)
        tele = dict(data.get("telemetry") or {})
        tele["net"] = net_label
        tele["ip"] = ip or tele.get("ip")
        devices = dict(tele.get("devices") or {})
        for p in ("wifi", "eth", "sms"):
            devices[p] = await netctl.path_status(cfg, p)
        tele["devices"] = devices
        patch: dict = {"net": net_label, "ip": ip, "telemetry": tele}
        if res.get("ok"):
            patch["net_switch_at"] = time.time()
        status_store.update(path, **patch)
    except Exception as exc:                         # noqa: BLE001
        log.debug("status patch after net switch failed: %s", exc)

    return out
