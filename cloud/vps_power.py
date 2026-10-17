"""vps_power.py — intentional offline/online control, one instance per device.

/offline  → OS shutdown on the device, then the plug is switched off once the
            device is really down (no power wasted while it sleeps).
            Dead-man suppressed.
/online   → plug cycle (off → on) to boot, clear intentional-offline.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from hub import Hub
from power import SmartPlug

log = logging.getLogger("vps_power")

Notify = Callable[[str], Awaitable[None]]

WAKE_BOOT_GRACE_S = 300.0   # after a wake cycle: booting, not dead or "off"


class VpsPower:
    def __init__(self, hub: Hub, device_id: int, plug: SmartPlug,
                 notify: Notify | None = None, *,
                 cut_after_shutdown: bool = True,
                 cut_settle_s: float = 60.0,
                 cut_max_wait_s: float = 300.0,
                 cut_give_up_s: float = 900.0,
                 cut_poll_s: float = 10.0) -> None:
        self._hub = hub
        self._dev = hub.device(device_id)
        self._plug = plug
        self._notify = notify
        self._cut_after_shutdown = cut_after_shutdown
        # The agent link must be gone this long before the power goes: a
        # hard cut in the middle of the OS shutdown can corrupt files.
        self._cut_settle_s = cut_settle_s
        # Cut after this long even while power is still drawn (a shut-down
        # laptop keeps charging its battery).
        self._cut_max_wait_s = cut_max_wait_s
        # The device never went down: leave the plug on.
        self._cut_give_up_s = cut_give_up_s
        self._cut_poll_s = cut_poll_s
        self._cut_task: asyncio.Task | None = None

    @property
    def _label(self) -> str:
        return self._dev.label

    async def offline(self) -> dict:
        """Shut down the device OS and keep it off until explicit open."""
        self._dev.intentional_offline = True
        self._dev.persist_offline_flag()

        res = await self._shutdown()
        self._start_power_cut()
        return res

    async def _shutdown(self) -> dict:
        shutdown_res: dict | None = None
        try:
            shutdown_res = await self._hub.send_command(
                self._dev.id, "action", "shutdown", {}, timeout=15.0)
        except RuntimeError:
            msg = (
                f"{self._label} marked OFFLINE — already down or unreachable. "
                "Use /online or wait for scheduled open to boot."
            )
            log.info(msg)
            if self._notify:
                await self._notify(f"🔴 {msg}")
            return {"ok": True, "state": "offline", "shutdown": "skipped"}
        except TimeoutError:
            # WS often drops before result when shutdown starts — expected
            msg = (f"{self._label} shutting down — stays off until /online "
                   "or scheduled open.")
            log.info(msg)
            if self._notify:
                await self._notify(f"🔴 {msg}")
            return {"ok": True, "state": "offline", "shutdown": "sent"}

        if shutdown_res.get("ok"):
            msg = (f"{self._label} shutting down — stays off until /online "
                   "or scheduled open.")
            log.info(msg)
            if self._notify:
                await self._notify(f"🔴 {msg}")
            return {"ok": True, "state": "offline", "shutdown": shutdown_res}

        err = shutdown_res.get("error") or shutdown_res.get("output") or "?"
        msg = f"{self._label} marked OFFLINE but shutdown failed: {err}"
        log.warning(msg)
        if self._notify:
            await self._notify(f"🔴 {msg}")
        return {"ok": True, "state": "offline", "shutdown": shutdown_res}

    # ── plug off once the device is down ──────────────────────────────────

    @property
    def cut_pending(self) -> bool:
        return self._cut_task is not None and not self._cut_task.done()

    def _start_power_cut(self) -> None:
        if not self._cut_after_shutdown or not self._plug.configured:
            return
        if self._cut_task and not self._cut_task.done():
            self._cut_task.cancel()
        self._cut_task = asyncio.create_task(self._cut_power())

    async def _watts(self) -> float | None:
        try:
            st = await self._plug.status()
        except Exception:                              # noqa: BLE001
            return None
        if not st.get("ok") or not st.get("online"):
            return None
        if not st.get("socket"):
            return 0.0
        if st.get("watts") is None:
            return None
        return float(st["watts"])

    async def _cut_power(self) -> None:
        started = time.time()
        down_since: float | None = None
        min_watts = self._plug.min_watts
        while True:
            await asyncio.sleep(self._cut_poll_s)
            if not self._dev.intentional_offline:
                log.info("%s: woken before the plug cut — plug left on",
                         self._label)
                return
            now = time.time()
            if self._dev.control is not None:
                if down_since is not None:
                    log.info("%s: agent came back — plug left on", self._label)
                    return
                if now - started >= self._cut_give_up_s:
                    log.warning("%s: never went down — plug left on",
                                self._label)
                    if self._notify:
                        await self._notify(
                            f"⚠️ {self._label} is still connected "
                            f"{int(self._cut_give_up_s // 60)} min after the "
                            "shutdown — plug left on.")
                    return
                continue
            if down_since is None:
                down_since = now
            if now - down_since < self._cut_settle_s:
                continue
            if self._dev.power_hold or self._dev.peer_seen:
                continue                   # health hold or the other device
                                           # still sees it: never cut now
            if now - started < self._cut_max_wait_s:
                watts = await self._watts()
                if watts is None or watts > min_watts:
                    continue
            break

        res = await self._plug.off()
        if res.get("ok"):
            log.info("%s: plug switched off after shutdown", self._label)
            if self._notify:
                await self._notify(
                    f"🔌 {self._label} is off — plug switched off. "
                    "It powers back on when a task arrives.")
        else:
            log.warning("%s: plug off failed: %s", self._label,
                        res.get("error"))
            if self._notify:
                await self._notify(
                    f"⚠️ {self._label} is off but the plug could not be "
                    f"switched off: {res.get('error', '?')}")

    async def online(self, *, notify: bool = True) -> dict:
        """Wake the device: plug cycle (off → on)."""
        if self._cut_task and not self._cut_task.done():
            self._cut_task.cancel()
        was_manual = self._dev.intentional_offline
        self._dev.intentional_offline = False
        self._dev.persist_offline_flag()

        if not self._plug.configured:
            msg = (f"{self._label} opening (plug not configured — power it "
                   "on manually)")
            log.warning(msg)
            if notify and self._notify:
                await self._notify(f"🟢 {msg}")
            return {"ok": True, "state": "online", "cycled": True, "stub": True}

        if notify and self._notify:
            reason = "scheduled open" if not was_manual else "manual /online"
            await self._notify(
                f"🟡 Waking {self._label} ({reason}) — plug off → on…")

        res = await self._plug.cycle()
        if res.get("ok"):
            self._dev.suppress_deadman_until = max(
                self._dev.suppress_deadman_until, time.time() + WAKE_BOOT_GRACE_S)
            if notify and self._notify:
                await self._notify(
                    f"🟢 {self._label} wake cycle done — waiting for boot…")
        else:
            self._dev.intentional_offline = was_manual
            self._dev.persist_offline_flag()
            if notify and self._notify:
                await self._notify(
                    f"Wake FAILED on {self._label}: {res.get('error', '?')}")
        return res
