"""anydesk_watchdog.py — keep AnyDesk running; restart if it crashed."""
from __future__ import annotations

import asyncio
import logging

import anydesk_mgr
from config import Config

log = logging.getLogger("anydesk_watch")


async def ensure_running(cfg: Config) -> dict:
    """Start AnyDesk if it is missing or not running as Administrator."""
    if not cfg.anydesk_watch_enabled:
        return {"ok": True, "skipped": True, "output": "watchdog disabled"}
    if not anydesk_mgr.is_running():
        log.warning("AnyDesk closed or missing — restarting as Administrator")
        res = await anydesk_mgr.start(cfg)
        if not res.get("ok"):
            log.error("AnyDesk start failed: %s", res.get("error") or res)
        else:
            log.info("AnyDesk started (elevated=%s id=%s)",
                     res.get("elevated"), res.get("id"))
        return res
    if anydesk_mgr.needs_admin_restart():
        log.warning("AnyDesk not elevated — restarting as Administrator")
        res = await anydesk_mgr.restart(cfg)
        if not res.get("ok"):
            log.error("AnyDesk admin restart failed: %s", res.get("error") or res)
        else:
            log.info("AnyDesk restarted elevated (id=%s)", res.get("id"))
        return res
    return {
        "ok": True,
        "output": "running",
        "elevated": anydesk_mgr.is_elevated(),
        "id": anydesk_mgr.read_id(cfg),
    }


class AnyDeskWatchdog:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    async def run(self) -> None:
        if not self._cfg.anydesk_watch_enabled:
            log.info("anydesk watchdog disabled")
            return
        log.info("anydesk watchdog started (interval=%ss)",
                 self._cfg.anydesk_watch_interval)
        while True:
            try:
                await ensure_running(self._cfg)
            except Exception as exc:                 # noqa: BLE001
                log.warning("anydesk watchdog error: %s", exc)
            await asyncio.sleep(self._cfg.anydesk_watch_interval)
