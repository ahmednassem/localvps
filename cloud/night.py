"""night.py — idle shutdown + wake-on-task, per device.

The lsvc task gate pings us over loopback, naming the device:

  POST /api/night/keepalive  {"device": N}   task/result  → resets the idle timer
  POST /api/night/wake       {"device": N}   task while off → plug-cycle boot

If NIGHT_IDLE_MINUTES (30) pass with no keepalive, the device is shut down via
the agent (intentional-offline, so the dead-man does not power it back). A task
that needs it boots it again through /wake.

Two modes:
  * NIGHT_ALWAYS=true  — the idle rule applies all day. This is the normal mode:
    there is no day or night shift, a device stays up only while work flows.
    /stay_online then holds until it is turned off by hand.
  * NIGHT_ALWAYS unset — the old window (NIGHT_START..NIGHT_END, local time).
    The timer starts fresh when the window opens and any pending shutdown is
    void the moment the clock leaves it. /stay_online resets at window end.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from hub import Hub
from vps_power import VpsPower

log = logging.getLogger("night")

Notify = Callable[[str], Awaitable[None]]


class NightWatch:
    def __init__(self, hub: Hub, device_id: int, vps: VpsPower,
                 notify: Notify, *,
                 tz: str = "UTC",
                 start_hour: int = 23,
                 end_hour: int = 9,
                 idle_minutes: float = 30.0,
                 poll: float = 30.0,
                 wake_cooldown: float = 300.0,
                 boot_grace: float = 300.0,
                 shutdown_cooldown: float = 300.0,
                 always: bool = False) -> None:
        self._hub = hub
        self._dev = hub.device(device_id)
        self._vps = vps
        self._notify = notify
        try:
            self._tz = ZoneInfo(tz)
        except Exception:
            log.warning("invalid night tz %r — using UTC", tz)
            self._tz = ZoneInfo("UTC")
        self._start_hour = start_hour
        self._end_hour = end_hour
        self._idle_seconds = max(idle_minutes, 1.0) * 60.0
        self._poll = poll
        self._wake_cooldown = wake_cooldown
        self._boot_grace = boot_grace
        self._shutdown_cooldown = shutdown_cooldown

        self._last_activity = 0.0
        self._was_in_window = False
        self._last_wake_at = 0.0
        self._last_shutdown_at = 0.0
        self.enabled = idle_minutes > 0
        # always=True: no window at all, the idle rule applies 24/7.
        self._always = always
        # /stay_online: suppress the idle shutdown; auto-resets when the
        # night window ends, so it covers one night at a time.
        self._stay_online = False

    # ── window helpers ──────────────────────────────────────────────────

    def in_window(self, now: datetime | None = None) -> bool:
        """True while the idle rule applies (always, or inside the window)."""
        if self._always:
            return True
        h = (now or datetime.now(self._tz)).hour
        if self._start_hour > self._end_hour:          # wraps midnight
            return h >= self._start_hour or h < self._end_hour
        return self._start_hour <= h < self._end_hour

    def _idle_for(self) -> float:
        if self._last_activity <= 0:
            return 0.0
        return time.time() - self._last_activity

    def snapshot(self) -> dict:
        return {
            "ok": True,
            "device": self._dev.id,
            "night": self.in_window(),
            "always": self._always,
            "online": self._dev.online(),
            "intentional_offline": self._dev.intentional_offline,
            "idle_seconds": round(self._idle_for(), 1),
            "idle_limit_seconds": self._idle_seconds,
            "enabled": self.enabled,
            "stay_online": self._stay_online,
        }

    @property
    def stay_online(self) -> bool:
        return self._stay_online

    def set_stay_online(self, on: bool) -> dict:
        """Toggle the night-idle suppression (Telegram /stay_online)."""
        self._stay_online = bool(on)
        if not on:
            # re-arm with a fresh timer so switching off never fires an
            # immediate shutdown from stale idle time
            self._last_activity = time.time()
        log.info("stay_online set to %s", self._stay_online)
        return self.snapshot()

    # ── called from the gate API (lsvc gate over loopback) ───────────────

    def keepalive(self) -> dict:
        """A task/result just moved — the laptop is earning its power."""
        self._last_activity = time.time()
        return self.snapshot()

    async def wake(self) -> dict:
        """Task arrived while the laptop is (probably) off — boot it."""
        self._last_activity = time.time()
        now = time.time()

        if now - self._last_wake_at < self._wake_cooldown:
            # already cycled recently — machine is booting, don't cut power
            return {"ok": True, "waking": True, "cooldown": True,
                    **self.snapshot()}

        need_boot = self._dev.intentional_offline or not self._dev.online()
        if not need_boot:
            # status is fresh: machine on, worker likely reconnecting
            return {"ok": True, "online": True, **self.snapshot()}

        self._last_wake_at = now
        # give the boot time before the dead-man considers another plug cycle
        self._dev.suppress_deadman_until = now + self._boot_grace
        await self._notify(
            f"🌙 Night wake: task arrived — powering on {self._dev.label}…")
        res = await self._vps.online(notify=False)
        if not res.get("ok"):
            self._last_wake_at = 0.0
            await self._notify(
                f"🌙 Night wake FAILED: {res.get('error', '?')}")
        return {"ok": bool(res.get("ok")), "waking": True, **self.snapshot()}

    # ── idle loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        if not self.enabled:
            log.info("night watch disabled (NIGHT_IDLE_MINUTES=0)")
            return
        if self._always:
            log.info("idle watch started (24/7, idle=%ss)", self._idle_seconds)
        else:
            log.info(
                "night watch started (window %02d:00-%02d:00 %s, idle=%ss)",
                self._start_hour, self._end_hour, self._tz.key,
                self._idle_seconds)
        while True:
            try:
                await self._tick()
            except Exception as exc:                    # noqa: BLE001
                log.warning("night tick error: %s", exc)
            await asyncio.sleep(self._poll)

    async def _tick(self) -> None:
        inside = self.in_window()
        if not inside:
            # window over: any pending idle shutdown is void (hard stop)
            if self._was_in_window and self._stay_online:
                self._stay_online = False
                log.info("night window ended — stay_online reset")
                await self._notify(
                    "🌙 Night ended — stay-online reset; idle shutdown is "
                    "armed again for the next night.")
            self._was_in_window = False
            return

        if not self._was_in_window:
            # window just opened (or gate restarted mid-window): fresh timer
            self._was_in_window = True
            self._last_activity = time.time()
            log.info("night window open — idle timer armed")
            return

        if self._stay_online:
            return                                       # admin wants it on all night
        if self._dev.intentional_offline or not self._dev.online():
            return                                       # already off
        if self._dev.power_hold:
            return                                       # router/plug failure: keep it up
        if time.time() - self._last_wake_at < self._boot_grace:
            return                                       # just woke it, let it work
        if self._idle_for() < self._idle_seconds:
            return
        if time.time() - self._last_shutdown_at < self._shutdown_cooldown:
            return                                       # shutdown already in flight

        self._last_shutdown_at = time.time()
        mins = int(self._idle_seconds // 60)
        await self._notify(
            f"🌙 Night idle: no tasks for {mins} min — shutting down "
            f"{self._dev.label}. It wakes automatically when a task arrives.")
        try:
            await self._vps.offline()
        except Exception as exc:                        # noqa: BLE001
            log.warning("night shutdown failed: %s", exc)
            await self._notify(f"🌙 Night shutdown FAILED: {exc}")
