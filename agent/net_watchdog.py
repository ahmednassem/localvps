"""net_watchdog.py — uplink health + failover / failback.

Rules:
  - "wifi" always means DSL main Wi‑Fi — never call SMS "wifi".
  - Internet dies on current path → Telegram alert (if on DSL: main Wi‑Fi OFF)
    then auto-switch to next available path + notify.
  - Failover/failback are relative to the HOME path, which is whatever the user
    last chose (net_pref), defaulting to Wi‑Fi DSL. If home is SMS, a failover
    to Wi‑Fi is temporary and we come back to SMS once SMS works again.
  - Automatic switches never rewrite net_pref (`remember=False`). Persisting a
    temporary hop is what used to silently convert a deliberate "stay on SMS"
    into "wifi", which net_startup then re-asserted on every wake.
  - Ticks are skipped while router_mgr is rebooting a box: during that window
    the route is parked on the survivor on purpose.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import netctl
import net_pref
from config import Config

log = logging.getLogger("net_watchdog")

Notify = Callable[[str], Awaitable[None]]

LABEL = {
    "wifi": "main Wi‑Fi (DSL)",
    "eth": "Ethernet",
    "sms": "SMS backup",
}


def _label(src: str) -> str:
    key = (src or "").split(":")[0].lower()
    return LABEL.get(key, src or "unknown")


class NetWatchdog:
    def __init__(self, cfg: Config, notify: Optional[Notify] = None) -> None:
        self._cfg = cfg
        self._notify = notify
        self._fails = 0
        self._last_switch = 0.0
        self._last_source = ""
        self._warned_down = False
        self._warned_no_alt = False
        # True only after an automatic failover away from home — enables failback
        self._auto_failover = False
        self._failover_from = ""

    def _home(self) -> str:
        """The path we belong on: the user's sticky choice, else Wi‑Fi DSL."""
        pref = (net_pref.get() or "").split(":")[0].lower()
        return pref if pref in ("wifi", "eth", "sms") else "wifi"

    async def _alert(self, msg: str) -> None:
        log.warning(msg)
        if not self._notify:
            return
        try:
            await self._notify(msg)
        except Exception:                            # noqa: BLE001
            pass

    async def _next_available(self, current: str) -> str | None:
        cur = (current or "").split(":")[0]
        home = self._home()
        if cur == "wifi":
            # Leaving DSL: cable first (the new router — its own WAN), then SMS.
            order = ["eth", "sms", "wifi"]
        elif cur == home:
            # Leaving a non-DSL home: anything but home.
            order = [s for s in netctl.FAILOVER_ORDER if s != home] + [home]
        else:
            # Not home already — try to get back home first.
            order = [home] + [s for s in netctl.FAILOVER_ORDER if s != home]
        configured = netctl.configured_sources(self._cfg)
        order = [s for s in dict.fromkeys(order) if s in configured and s != cur]
        for cand in order:
            st = await netctl.path_status(self._cfg, cand)
            if netctl.path_is_up(st):
                return cand
        return None

    async def run(self) -> None:
        if not self._cfg.net_watch_enabled:
            log.info("net watchdog disabled")
            return
        log.info(
            "net watchdog started (interval=%ss) — main Wi‑Fi DSL, "
            "failover eth/SMS, failback to DSL",
            self._cfg.net_watch_interval,
        )
        while True:
            try:
                await self._tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("net watchdog tick error: %s", exc)
                await self._alert(f"⚠ Net watchdog error: {exc}")
            await asyncio.sleep(self._cfg.net_watch_interval)

    async def _tick(self) -> None:
        import router_mgr

        busy = router_mgr.reboot_in_progress()
        if busy:
            log.debug("skipping tick: %s reboot in progress", busy)
            return

        src, ip = await netctl.active_source(self._cfg)
        self._last_source = src
        cur = (src or "").split(":")[0]
        home = self._home()

        # After an auto failover away from home: return home once it recovers
        if self._auto_failover and cur != home:
            await self._try_failback_home()
            src, ip = await netctl.active_source(self._cfg)
            cur = (src or "").split(":")[0]

        ok = await netctl.has_internet(
            self._cfg.net_check_host, self._cfg.net_check_port,
            timeout=self._cfg.net_check_timeout,
        )
        if ok:
            if cur == home:
                self._auto_failover = False
                self._failover_from = ""
            if self._fails or self._warned_down:
                log.info("internet OK on %s (%s)", src, ip)
                await self._alert(
                    f"Internet OK again on {_label(src)} ({ip or 'no-ip'})")
            self._fails = 0
            self._warned_down = False
            self._warned_no_alt = False
            return

        self._fails += 1
        log.warning("no internet on %s (fail %d/%d)", src, self._fails,
                    self._cfg.net_fail_threshold)
        if self._fails == self._cfg.net_fail_threshold and not self._warned_down:
            self._warned_down = True
            if cur == "wifi":
                await self._alert(
                    f"⚠ Main Wi‑Fi (DSL) is OFF / no internet "
                    f"({ip or 'no-ip'}) — failing over"
                )
            else:
                await self._alert(
                    f"⚠ Internet DOWN on {_label(src)} ({ip or 'no-ip'}) "
                    f"— failing over"
                )
        if self._fails < self._cfg.net_fail_threshold:
            return

        now = time.time()
        if now - self._last_switch < self._cfg.net_switch_cooldown:
            return

        nxt = await self._next_available(src)
        if not nxt:
            if not self._warned_no_alt:
                self._warned_no_alt = True
                await self._alert(
                    f"⚠ Internet down on {_label(src)} — "
                    "no alternate path available"
                )
            return

        self._fails = 0
        self._last_switch = now
        await self._alert(
            f"Auto-switching from {_label(src)} → {_label(nxt)}"
        )

        # remember=False: this is a temporary hop, not the user's choice.
        res = await netctl.switch(self._cfg, nxt, remember=False)
        if not res.get("ok"):
            await self._alert(
                f"⚠ Failover to {_label(nxt)} FAILED: "
                f"{res.get('error') or res.get('output') or res}"
            )
            return

        if cur == self._home():
            self._auto_failover = True
            self._failover_from = cur

        await asyncio.sleep(self._cfg.net_post_switch_wait)
        new_src, new_ip = await netctl.active_source(self._cfg)
        if await netctl.has_internet(self._cfg.net_check_host,
                                     self._cfg.net_check_port,
                                     timeout=self._cfg.net_check_timeout):
            log.info("failover to %s ok (now %s / %s)", nxt, new_src, new_ip)
            await self._alert(
                f"Now using {_label(new_src)} ({new_ip or 'no-ip'})"
            )
            self._warned_down = False
            self._warned_no_alt = False
        else:
            await self._alert(
                f"⚠ Switched to {_label(nxt)} but still NO INTERNET"
            )

    async def _try_failback_home(self) -> None:
        """After an automatic failover, return to the home path once it works."""
        home = self._home()
        if home == "wifi" and not self._cfg.wifi_dsl_ssid:
            return
        if home == "sms" and not self._cfg.wifi_sms_ssid:
            return
        now = time.time()
        if now - self._last_switch < self._cfg.net_switch_cooldown:
            return

        # Only fail back if the home path is selectable at all
        st = await netctl.path_status(self._cfg, home)
        if not netctl.path_is_up(st):
            return

        src_before, _ = await netctl.active_source(self._cfg)
        cur_before = (src_before or "").split(":")[0]

        log.info("probing %s for failback home", home)
        res = await netctl.switch(self._cfg, home, remember=False)
        if not res.get("ok"):
            # Home still unreachable — stay where we were.
            if cur_before in ("wifi", "eth", "sms") and cur_before != home:
                await netctl.switch(self._cfg, cur_before, remember=False)
            return

        await asyncio.sleep(self._cfg.net_post_switch_wait)
        if await netctl.has_internet(self._cfg.net_check_host,
                                     self._cfg.net_check_port,
                                     timeout=self._cfg.net_check_timeout):
            self._last_switch = now
            self._auto_failover = False
            self._failover_from = ""
            new_src, new_ip = await netctl.active_source(self._cfg)
            await self._alert(
                f"{_label(home)} is back — switched to {_label(new_src)} "
                f"({new_ip or 'no-ip'})"
            )
        else:
            await self._alert(
                f"⚠ {_label(home)} associated but no internet — "
                "staying on backup"
            )
            nxt = await self._next_available(home)
            if nxt:
                await netctl.switch(self._cfg, nxt, remember=False)
