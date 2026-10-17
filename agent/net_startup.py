"""Restore the default network path at boot / resume from sleep."""
from __future__ import annotations

import logging

import net_pref
import netctl
import net_status
from config import Config

log = logging.getLogger("net_startup")


async def ensure_default_network(
    cfg: Config,
    *,
    reason: str = "startup",
    patch_status: bool = True,
) -> dict:
    """Wi‑Fi DSL is the default. A deliberate SMS or eth preference survives
    reboot / wake (eth is the new router — its own WAN, a real choice now)."""
    pref = net_pref.get()
    if pref in ("sms", "eth"):
        log.info("%s: restoring preferred %s", reason, pref)
        res = await netctl.switch(cfg, pref)
        label = pref
    else:
        log.info("%s: restoring default Wi‑Fi (DSL)", reason)
        res = await netctl.prefer_wifi(cfg)
        net_pref.set_source("wifi")
        label = "wifi"
    if patch_status and res.get("ok"):
        res = await net_status.apply_after_switch(cfg, label, res)
    return res
