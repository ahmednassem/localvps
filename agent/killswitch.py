"""killswitch.py — autonomous overheat protection.

Runs inside the always-on service and does NOT depend on the VPS connection.
If the hottest sensor stays above MAX_TEMP_C continuously for TEMP_GRACE seconds,
it performs a graceful shutdown to protect the hardware. A best-effort notify
callback (to the VPS) is attempted first, but shutdown proceeds regardless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import hwtemp
from config import Config
from winutil import run

log = logging.getLogger("killswitch")

Notify = Callable[[str], Awaitable[None]]


class KillSwitch:
    def __init__(self, cfg: Config, notify: Optional[Notify] = None,
                 poll: float = 10.0) -> None:
        self._cfg = cfg
        self._notify = notify
        self._poll = poll
        self._hot_since: float | None = None
        self._fired = False

    async def run(self) -> None:
        log.info("kill switch armed (max=%.0fC, grace=%.0fs)",
                 self._cfg.max_temp_c, self._cfg.temp_grace)
        while True:
            try:
                await self._tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("killswitch tick error: %s", exc)
            await asyncio.sleep(self._poll)

    async def _tick(self) -> None:
        if self._fired:
            return
        temp = hwtemp.read_temp_c()
        if temp is None:
            self._hot_since = None
            return
        if temp < self._cfg.max_temp_c:
            self._hot_since = None
            return

        now = time.time()
        if self._hot_since is None:
            self._hot_since = now
            log.warning("temp %.0fC over limit %.0fC — grace started",
                        temp, self._cfg.max_temp_c)
            return

        if now - self._hot_since >= self._cfg.temp_grace:
            await self._trip(temp)

    async def _trip(self, temp: float) -> None:
        self._fired = True
        msg = (f"KILL SWITCH: temp {temp:.0f}C exceeded {self._cfg.max_temp_c:.0f}C "
               f"for {self._cfg.temp_grace:.0f}s — shutting down to protect hardware.")
        log.critical(msg)
        if self._notify is not None:
            try:
                await asyncio.wait_for(self._notify(msg), timeout=5)
            except Exception:                        # noqa: BLE001
                pass
        # graceful shutdown; /t 0 immediate, /f force-close apps
        await run("shutdown /s /f /t 0", timeout=15)
