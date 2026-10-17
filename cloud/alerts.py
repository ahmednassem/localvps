"""alerts.py — health watchdog + dead-man recovery, one instance per device.

Runs a loop that:
  - warns when something looks wrong (no internet, command channel down, high
    CPU/RAM/disk, AnyDesk/RustDesk stopped, high temperature, offline);
  - power-cycles the device's smart plug when it stops sending status
    (dead-man);
  - notifies when the device recovers;
  - optionally sends a Telegram status digest every STATUS_REPORT_INTERVAL
    (pass status_digest on ONE instance only, or every device sends its own).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from hub import Hub
from power import SmartPlug

log = logging.getLogger("alerts")

Notify = Callable[[str], Awaitable[None]]


class Watchdog:
    def __init__(self, hub: Hub, device_id: int, plug: SmartPlug,
                 notify: Notify, *,
                 grace: float = 150.0,
                 poll: float = 15.0,
                 temp_warn_c: float = 85.0,
                 recover_cooldown: float = 1800.0,   # at most 2 cycles an hour
                 status_report_interval: float = 1800.0,
                 cpu_warn: float = 90.0,
                 ram_warn: float = 90.0,
                 disk_warn: float = 90.0,
                 battery_low: float = 20.0,
                 status_digest: Callable[[], Awaitable[None]] | None = None) -> None:
        self._hub = hub
        self._dev = hub.device(device_id)
        self._plug = plug
        self._notify = notify
        self._status_digest = status_digest
        self._grace = grace
        self._poll = poll
        self._temp_warn_c = temp_warn_c
        self._recover_cooldown = recover_cooldown
        self._status_report_interval = status_report_interval
        self._cpu_warn = cpu_warn
        self._ram_warn = ram_warn
        self._disk_warn = disk_warn
        self._battery_low = battery_low

        self._deadman_active = False
        self._last_recover_at = 0.0
        self._last_status_report = time.time()
        self._was_online = False
        self._started_at = time.time()
        self._never_seen_grace = 900.0    # 15 min before dead-man on a
                                          # device not seen since gate start

        # warning latches (notify once until cleared)
        self._temp_warned = False
        self._warn_internet = False
        self._warn_cmd = False
        self._warn_anydesk = False
        self._warn_rustdesk = False
        self._warn_cpu = False
        self._warn_ram = False
        self._warn_disk = False
        self._warn_battery = False

    @property
    def _label(self) -> str:
        return self._dev.label

    async def run(self) -> None:
        log.info(
            "watchdog[%s] started (grace=%ss, poll=%ss, temp_warn=%s, status_every=%ss)",
            self._label, self._grace, self._poll, self._temp_warn_c,
            self._status_report_interval,
        )
        while True:
            try:
                await self._tick()
            except Exception as exc:            # noqa: BLE001
                log.warning("watchdog[%s] tick error: %s", self._label, exc)
            await asyncio.sleep(self._poll)

    def _telemetry(self) -> dict:
        return self._dev.telemetry()

    async def _tick(self) -> None:
        if (self._dev.http_last_seen <= 0 and self._dev.control is None
                and time.time() - self._started_at < self._never_seen_grace):
            # not seen since the gate started (agent not installed yet, or
            # the gate just restarted) — hold the dead-man for a grace
            # period instead of plug-cycling immediately on boot.
            await self._maybe_status_report()
            return
        gap = self._dev.seconds_since_status()
        online = self._dev.online() and gap <= self._grace

        # recovery edge: was silent, now healthy again
        if online and not self._was_online:
            await self._power_plug_if_off()
            if self._dev.intentional_offline:
                self._dev.intentional_offline = False
                self._dev.persist_offline_flag()
                await self._notify(
                    f"✅ {self._label} back online — manual-offline cleared "
                    "(plug powered on).")
            elif self._deadman_active:
                await self._notify(
                    f"✅ {self._label} is back online — status resumed.")
            self._deadman_active = False
            self._temp_warned = False
        self._was_online = online

        # scheduled status digest every N seconds
        await self._maybe_status_report()

        if not online:
            if gap > self._grace:
                if self._dev.intentional_offline:
                    if not self._deadman_active:
                        self._deadman_active = True
                        await self._notify(
                            f"🔴 {self._label} is OFFLINE (manual shutdown) — "
                            "dead-man recovery disabled. Use /online to boot."
                        )
                    return
                if time.time() < self._dev.suppress_deadman_until:
                    # a wake just cycled the plug — give the boot its grace
                    return
                if self._dev.deadman_ok is False:
                    # health says this is not case 5 (device off, plug off,
                    # router/plug failure, ...): no plug cycle
                    return
                await self._handle_deadman(gap)
            return

        await self._check_health_warnings()

    async def _power_plug_if_off(self) -> None:
        """The device is running but its plug is off (it booted by itself on
        battery: power button, BIOS power-on schedule): give it power. The
        plug is never switched on while the device is off."""
        if not self._plug.configured:
            return
        st = await self._plug.status()
        if not st.get("ok") or not st.get("online") or st.get("socket"):
            return
        res = await self._plug.on()
        if res.get("ok"):
            log.info("%s came up with the plug off — plug switched on",
                     self._label)
            await self._notify(
                f"🔌 {self._label} turned on by itself — plug switched on.")
        else:
            await self._notify(
                f"⚠️ {self._label} is running on battery: plug on failed: "
                f"{res.get('error', '?')}")

    async def _maybe_status_report(self) -> None:
        if self._status_report_interval <= 0 or self._status_digest is None:
            return
        now = time.time()
        if now - self._last_status_report < self._status_report_interval:
            return
        self._last_status_report = now
        try:
            await self._status_digest()
        except Exception as exc:                # noqa: BLE001
            log.warning("status digest failed: %s", exc)

    async def _check_health_warnings(self) -> None:
        tele = self._telemetry()
        await self._check_temp(tele)
        await self._check_internet(tele)
        await self._check_command_channel()
        await self._check_anydesk(tele)
        await self._check_rustdesk(tele)
        await self._check_resources(tele)
        await self._check_battery(tele)

    async def _warn_edge(self, flag: str, active: bool, message: str,
                         *, recovery: str = "") -> None:
        """Set a bool attribute; notify on rising/falling edge."""
        cur = getattr(self, flag)
        if active and not cur:
            setattr(self, flag, True)
            await self._notify(f"⚠️ WARNING [{self._label}]: {message}")
        elif not active and cur:
            setattr(self, flag, False)
            if recovery:
                await self._notify(f"✅ [{self._label}] {recovery}")

    async def _check_internet(self, tele: dict) -> None:
        inet = tele.get("internet")
        if inet is None:
            return
        await self._warn_edge(
            "_warn_internet", inet is False,
            "no internet connection.",
            recovery="internet restored.",
        )

    async def _check_command_channel(self) -> None:
        down = self._dev.control is None
        await self._warn_edge(
            "_warn_cmd", down,
            "command channel down (WebSocket). "
            "Status works but commands may fail.",
            recovery="command channel restored.",
        )

    async def _check_anydesk(self, tele: dict) -> None:
        ad = tele.get("anydesk") or {}
        stopped = not ad.get("running")
        await self._warn_edge(
            "_warn_anydesk", stopped,
            "AnyDesk is not running.",
            recovery="AnyDesk is running again.",
        )

    async def _check_rustdesk(self, tele: dict) -> None:
        rd = tele.get("rustdesk")
        if rd is None:
            return  # no signal, no warning
        stopped = not rd.get("running")
        await self._warn_edge(
            "_warn_rustdesk", stopped,
            "RustDesk is not running.",
            recovery="RustDesk is running again.",
        )

    async def _check_resources(self, tele: dict) -> None:
        cpu = tele.get("cpu")
        if isinstance(cpu, (int, float)):
            await self._warn_edge(
                "_warn_cpu", cpu >= self._cpu_warn,
                f"CPU usage high: {cpu:.0f}%",
                recovery=f"CPU back to normal ({cpu:.0f}%).",
            )
        ram = tele.get("ram")
        if isinstance(ram, (int, float)):
            await self._warn_edge(
                "_warn_ram", ram >= self._ram_warn,
                f"RAM usage high: {ram:.0f}%",
                recovery=f"RAM back to normal ({ram:.0f}%).",
            )
        disk = tele.get("disk")
        if isinstance(disk, (int, float)):
            await self._warn_edge(
                "_warn_disk", disk >= self._disk_warn,
                f"Disk usage high: {disk:.0f}%",
                recovery=f"Disk back to normal ({disk:.0f}%).",
            )

    async def _check_battery(self, tele: dict) -> None:
        batt = tele.get("battery")
        charging = tele.get("charging")
        if not isinstance(batt, (int, float)):
            return
        low = batt <= self._battery_low and not charging
        await self._warn_edge(
            "_warn_battery", low,
            f"battery low: {batt:.0f}% (not charging)",
            recovery=f"battery OK: {batt:.0f}%.",
        )

    async def _handle_deadman(self, gap: float) -> None:
        if self._deadman_active:
            return
        self._deadman_active = True
        gap_txt = "a long time" if gap == float("inf") else f"{int(gap)}s"
        await self._notify(
            f"⚠️ WARNING: {self._label} may be off/hung — no status for "
            f"{gap_txt}. Attempting power-cycle via the smart plug."
        )

        now = time.time()
        if now - self._last_recover_at < self._recover_cooldown:
            await self._notify(
                f"[{self._label}] Skipping power-cycle (attempted recently). "
                "Will keep watching."
            )
            return
        self._last_recover_at = now

        if not self._plug.configured:
            await self._notify(
                f"[{self._label}] Smart plug not configured — cannot "
                "auto-recover. Power-cycle the device manually."
            )
            return

        res = await self._plug.cycle()
        if res.get("ok"):
            await self._notify(
                f"[{self._label}] Smart plug power-cycled. Waiting for the "
                "core to boot…")
        else:
            await self._notify(
                f"[{self._label}] Power-cycle FAILED. Manual intervention "
                "needed.")

    async def _check_temp(self, tele: dict | None = None) -> None:
        tele = tele if tele is not None else self._telemetry()
        temp = tele.get("temp")
        if not isinstance(temp, (int, float)):
            return
        if temp >= self._temp_warn_c and not self._temp_warned:
            self._temp_warned = True
            await self._notify(
                f"⚠️ WARNING [{self._label}]: temperature high: {temp:.0f}°C")
        elif temp < self._temp_warn_c - 5 and self._temp_warned:
            self._temp_warned = False
            await self._notify(
                f"✅ [{self._label}] temperature back to normal: {temp:.0f}°C")
