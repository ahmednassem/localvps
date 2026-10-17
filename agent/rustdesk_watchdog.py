"""rustdesk_watchdog.py — keep RustDesk running; restart if it crashed."""
from __future__ import annotations

import asyncio
import logging

import rustdesk_mgr
from config import Config

log = logging.getLogger("rustdesk_watch")


async def ensure_running(cfg: Config) -> dict:
    """Start RustDesk if it is missing or not running as Administrator."""
    if not cfg.rustdesk_watch_enabled:
        return {"ok": True, "skipped": True, "output": "watchdog disabled"}
    if not rustdesk_mgr.is_running():
        log.warning("RustDesk closed or missing — restarting as Administrator")
        res = await rustdesk_mgr.start(cfg)
        if not res.get("ok"):
            log.error("RustDesk start failed: %s", res.get("error") or res)
        else:
            log.info("RustDesk started (elevated=%s id=%s)",
                     res.get("elevated"), res.get("id"))
        return res
    if rustdesk_mgr.needs_admin_restart():
        log.warning("RustDesk not elevated — restarting as Administrator")
        res = await rustdesk_mgr.restart(cfg)
        if not res.get("ok"):
            log.error("RustDesk admin restart failed: %s", res.get("error") or res)
        else:
            log.info("RustDesk restarted elevated (id=%s)", res.get("id"))
        return res
    return {
        "ok": True,
        "output": "running",
        "elevated": rustdesk_mgr.is_elevated(),
        "id": await rustdesk_mgr.read_id(cfg),
    }


class RustDeskWatchdog:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    async def run(self) -> None:
        if not self._cfg.rustdesk_watch_enabled:
            log.info("rustdesk watchdog disabled")
            return
        log.info("rustdesk watchdog started (interval=%ss)",
                 self._cfg.rustdesk_watch_interval)
        while True:
            try:
                await ensure_running(self._cfg)
            except Exception as exc:                 # noqa: BLE001
                log.warning("rustdesk watchdog error: %s", exc)
            await asyncio.sleep(self._cfg.rustdesk_watch_interval)
