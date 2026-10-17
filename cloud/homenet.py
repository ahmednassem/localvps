"""homenet.py — main router and SMS box state, from what the laptops and the
plugs report; plus the plug and home-network warnings.

Evidence (newest beat of each live laptop, last MQTT view of each plug):

  main router  ok           a laptop has internet over Wi-Fi/Ethernet, or a
                            plug is on the VPS through the router's Wi-Fi
               down         a plug fell back to the SMS box Wi-Fi, or a
                            laptop can neither reach the router nor see its
                            Wi-Fi
               no_internet  a laptop reaches the router but its Wi-Fi/
                            Ethernet has no internet, or a laptop sees a plug
                            on the LAN that can't reach the VPS
  SMS box      ok           a laptop sees its Wi-Fi/USB or is online through
                            it, or a plug is on the VPS through its Wi-Fi
               down         a laptop says it is gone
  home         unreachable  no laptop beat and no plug on the VPS

"ok" wins over "down" (a plug may stay on the backup Wi-Fi after the router
is back). A state must hold HOLD_S before it is announced; "unknown" never
replaces a known state.
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("homenet")

Notify = Callable[[str], Awaitable[None]]

HOLD_S = 90.0
PLUG_OFFLINE_ALERT_S = 120.0

ROUTER_TEXT = {"ok": "OK", "down": "DOWN", "no_internet": "up, no internet",
               "unknown": "unknown"}
SMS_TEXT = {"ok": "OK", "down": "DOWN", "unknown": "unknown"}


def assess(laptops: list[dict], plugs: list[dict]) -> dict:
    """laptops: label, seen, net, internet, router_ok, wifi, sms, sees_plugs.
    plugs: n, label, online, wifi (main | backup | other SSID | None)."""
    r_ok, r_down, r_noinet, s_ok, s_down = [], [], [], [], []
    for lp in laptops:
        if not lp.get("seen"):
            continue
        name = lp["label"]
        on_router = lp.get("net") in ("wifi", "eth")
        if on_router and lp.get("internet"):
            r_ok.append(f"{name} online through it")
        elif on_router and lp.get("internet") is False and lp.get("router_ok"):
            r_noinet.append(f"{name} reaches it, no internet")
        if lp.get("router_ok") is False and lp.get("wifi") == "off":
            r_down.append(f"{name} can't reach it or see its Wi-Fi")
        if lp.get("sms") == "on" or (lp.get("net") == "sms" and lp.get("internet")):
            s_ok.append(f"{name} sees it")
        elif lp.get("sms") == "off":
            s_down.append(f"{name} can't see it")
        for n in lp.get("sees_plugs") or ():
            p = next((p for p in plugs if p["n"] == n), None)
            if p is not None and not p.get("online") and p.get("wifi") != "backup":
                r_noinet.append(f"{name} sees {p['label']} on the LAN, it can't reach the VPS")
    for p in plugs:
        if not p.get("online"):
            continue
        if p.get("wifi") == "main":
            r_ok.append(f"{p['label']} on its Wi-Fi")
        elif p.get("wifi") == "backup":
            r_down.append(f"{p['label']} moved to the SMS box Wi-Fi")
            s_ok.append(f"{p['label']} on its Wi-Fi")
    if r_ok:
        router, r_why = "ok", r_ok
    elif r_down:
        router, r_why = "down", r_down
    elif r_noinet:
        router, r_why = "no_internet", r_noinet
    else:
        router, r_why = "unknown", []
    sms, s_why = ("ok", s_ok) if s_ok else ("down", s_down) if s_down else ("unknown", [])
    silent = (not any(lp.get("seen") for lp in laptops)
              and not any(p.get("online") for p in plugs))
    return {"router": router, "router_why": r_why, "sms": sms, "sms_why": s_why,
            "home": "unreachable" if silent else "ok"}


def _why(items: list[str]) -> str:
    return "; ".join(items[:3])


def plug_line(label: str, v: dict | None) -> str:
    """One status line for a plug's MQTT view."""
    if not v or not v.get("ok"):
        return f"🔌 {label}: no data ({(v or {}).get('error', 'not polled yet')})"
    if not v.get("online"):
        seen = v.get("seen_s")
        ago = f", last seen {seen // 60} min ago" if isinstance(seen, int) else ""
        return f"🔴 {label}: OFFLINE — not connected to the VPS{ago}"
    wifi = {"main": "main Wi-Fi", "backup": "SMS box Wi-Fi"}.get(v.get("wifi"),
                                                                v.get("wifi") or "Wi-Fi ?")
    sig = f" {v['signal']} dBm" if v.get("signal") is not None else ""
    power = (f" · {v['watts']} W" if v.get("watts") is not None else "") + (
        f" · {v['volts']} V" if v.get("volts") is not None else "")
    lamp = " · lamp on" if v.get("lamp") else ""
    badge = "🟠" if v.get("wifi") == "backup" else "🟢"
    return (f"{badge} {label}: socket {'ON' if v.get('socket') else 'OFF'}{power}"
            f"{lamp} · {wifi}{sig}")


class NetWatch:
    """Announces router / SMS box / home changes and per-plug changes."""

    def __init__(self, notify: Notify) -> None:
        self._notify = notify
        self.view: dict = {"router": "unknown", "sms": "unknown", "home": "ok"}
        self._stable: dict[str, str] = {}
        self._pending: dict[str, tuple[str, float]] = {}
        self._plug_online: dict[int, bool] = {}
        self._plug_wifi: dict[int, str | None] = {}
        self._plug_offline_since: dict[int, float] = {}
        self._plug_alerted: dict[int, bool] = {}

    def status_line(self) -> str:
        v = self.view
        line = (f"🌐 Home: main router {ROUTER_TEXT.get(v['router'], v['router'])}"
                f" · SMS box {SMS_TEXT.get(v['sms'], v['sms'])}")
        if v.get("home") == "unreachable":
            line += " · ⚠️ nothing at home reaches the VPS"
        return line

    async def update(self, view: dict, plugs: list[dict], *, announce: bool) -> None:
        self.view = view
        now = time.time()
        for key in ("router", "sms", "home"):
            await self._settle(key, view[key], view, now, announce)
        for p in plugs:
            await self._plug(p, now, announce)

    async def _settle(self, key: str, state: str, view: dict, now: float,
                      announce: bool) -> None:
        if state == "unknown":
            self._pending.pop(key, None)
            return
        old = self._stable.get(key)
        if state == old:
            self._pending.pop(key, None)
            return
        cand, since = self._pending.get(key, (state, now))
        if cand != state:
            cand, since = state, now
        self._pending[key] = (cand, since)
        if old is not None and now - since < HOLD_S:
            return
        self._stable[key] = state
        self._pending.pop(key, None)
        if old is None or not announce:
            return
        text = self._text(key, old, state, view)
        if text:
            log.info("%s: %s -> %s", key, old, state)
            await self._notify(text)

    @staticmethod
    def _text(key: str, old: str, new: str, view: dict) -> str | None:
        if key == "router":
            why = _why(view["router_why"])
            if new == "down":
                return (f"🚨 Main router is DOWN ({why}). Internet only through "
                        "the SMS box; plugs on its Wi-Fi stay reachable from the VPS.")
            if new == "no_internet":
                return (f"⚠️ Main router is up but has NO internet ({why}). Laptops "
                        "fail over to the SMS box; plugs on the router's Wi-Fi are "
                        "switched through a laptop.")
            return f"✅ Main router back ({why})."
        if key == "sms":
            why = _why(view["sms_why"])
            if new == "down":
                return f"⚠️ SMS box is DOWN ({why}) — no backup internet."
            return f"✅ SMS box back ({why})."
        if new == "unreachable":
            return ("🏠 Nothing at home reaches the VPS: both laptops and both plugs "
                    "are silent. Home internet (router and SMS box) or house "
                    "power is out.")
        return "✅ Home reachable again."

    async def _plug(self, p: dict, now: float, announce: bool) -> None:
        n, label = p["n"], p["label"]
        if not p.get("known"):
            return
        online, wifi = bool(p.get("online")), p.get("wifi")
        was = self._plug_online.get(n)
        self._plug_online[n] = online
        if online:
            self._plug_offline_since.pop(n, None)
            if self._plug_alerted.pop(n, False) and announce:
                await self._notify(f"✅ {label} back online — {plug_line(label, p.get('view'))[2:]}")
        elif was is not False:
            self._plug_offline_since[n] = now
        if (announce and not online and not self._plug_alerted.get(n)
                and now - self._plug_offline_since.get(n, now) >= PLUG_OFFLINE_ALERT_S):
            self._plug_alerted[n] = True
            lan = p.get("lan_by")
            tail = (f" {lan} still reaches it on the LAN, so switching goes through it."
                    if lan else " No laptop reaches it either: it can't be switched.")
            await self._notify(f"🔌 {label} is OFFLINE — not connected to the VPS.{tail}")
        if not online or wifi is None:
            return
        old_wifi = self._plug_wifi.get(n)
        key = f"wifi{n}"
        if old_wifi is None or wifi == old_wifi:
            self._plug_wifi[n] = wifi
            self._pending.pop(key, None)
            return
        cand, since = self._pending.get(key, (wifi, now))
        if cand != wifi:
            since = now
        self._pending[key] = (wifi, since)
        if now - since < HOLD_S:
            return
        self._pending.pop(key, None)
        self._plug_wifi[n] = wifi
        if not announce:
            return
        if wifi == "backup":
            await self._notify(f"📶 {label} moved to the SMS box Wi-Fi "
                               "(lost the main router's Wi-Fi). Still controlled from the VPS.")
        elif old_wifi == "backup" and wifi == "main":
            await self._notify(f"✅ {label} back on the main router Wi-Fi.")
