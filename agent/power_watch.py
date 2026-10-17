"""power_watch.py — warn when the AC adapter is unplugged / restored.

Polls Windows battery ACPI. On AC cut-off sends a critical notify including
current battery percent. On AC restore sends a short info notify.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import psutil

from config import Config

log = logging.getLogger("power_watch")

Notify = Callable[[str], Awaitable[None]]


class PowerWatch:
    def __init__(self, cfg: Config, notify: Optional[Notify] = None) -> None:
        self._cfg = cfg
        self._notify = notify
        self._last_plugged: bool | None = None
        self._interval = float(getattr(cfg, "power_watch_interval", 15) or 15)

    async def run(self) -> None:
        if not getattr(self._cfg, "power_watch_enabled", True):
            log.info("power watch disabled")
            return
        log.info("power watch started (interval=%ss)", self._interval)
        while True:
            try:
                await self._tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("power watch tick error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        batt = psutil.sensors_battery()
        if batt is None:
            return
        plugged = bool(batt.power_plugged)
        pct = int(round(batt.percent))

        if self._last_plugged is None:
            self._last_plugged = plugged
            log.info("AC initial: %s (battery %s%%)",
                     "plugged" if plugged else "on battery", pct)
            return

        if self._last_plugged and not plugged:
            msg = (
                f"⚠ AC adapter cut off — running on battery ({pct}%)"
            )
            log.warning(msg)
            if self._notify:
                try:
                    await self._notify(msg)
                except Exception:                    # noqa: BLE001
                    pass
        elif (not self._last_plugged) and plugged:
            msg = f"AC power restored — battery {pct}%"
            log.info(msg)
            if self._notify:
                try:
                    await self._notify(msg)
                except Exception:                    # noqa: BLE001
                    pass

        self._last_plugged = plugged
