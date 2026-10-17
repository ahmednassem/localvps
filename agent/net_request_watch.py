"""net_request_watch.py — elevated core applies UI network switch requests."""
from __future__ import annotations

import asyncio
import logging

import net_request
import netctl
import net_status
from config import Config

log = logging.getLogger("net_request")


class NetRequestWatch:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    async def run(self) -> None:
        log.info("net request watch started (UI → core switch queue)")
        while True:
            try:
                await self._tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("net request tick error: %s", exc)
            await asyncio.sleep(0.8)

    async def _tick(self) -> None:
        req = net_request.take()
        if not req:
            return
        source = (req.get("source") or "").lower().strip()
        rid = req.get("id") or ""
        log.info("UI net request id=%s source=%s", rid, source)

        blocked = await netctl.ensure_path_up(self._cfg, source)
        if blocked is not None:
            net_request.write_result(rid, source, blocked)
            return

        res = await netctl.switch(self._cfg, source)
        res = await net_status.apply_after_switch(self._cfg, source, res)
        net_label = res.get("net") or source
        ip = res.get("ip") or ""

        net_request.write_result(rid, source, res)
        log.info(
            "UI net request done id=%s ok=%s now=%s ip=%s",
            rid, res.get("ok"), net_label, ip,
        )
