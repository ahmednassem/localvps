"""plugs.py — both smart plugs (OpenBeken) over the home LAN.

Every node can read and switch plug 1 and plug 2 directly over HTTP
(/cm?cmnd=...), with no cloud and no broker. the VPS uses this through a
laptop when a plug is not connected to its MQTT broker. .env:

  PLUGn_HOST              plug n's address (fixed DHCP lease)
  PLUGn_PASS              web password, if set (user admin)
  PLUG_RELAY_CH           socket channel, default 1
  PLUG_LAMP_CH            night-lamp channel, default 2
  PLUG_MIN_WATTS          below = nothing drawing, default 5
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("plugs")

SOCKET_TIMEOUT_S = 4

_lock = threading.Lock()


def _host(n: int) -> str:
    return os.environ.get(f"PLUG{n}_HOST", "").strip()


def _ch(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)).strip() or default)


def configured(n: int) -> bool:
    return bool(_host(n))


def _cmd(n: int, cmnd: str) -> dict:
    """One console command over HTTP, JSON answer."""
    url = f"http://{_host(n)}/cm?cmnd={urllib.parse.quote(cmnd)}"
    req = urllib.request.Request(url)
    pw = os.environ.get(f"PLUG{n}_PASS", "").strip()
    if pw:
        tok = base64.b64encode(f"admin:{pw}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with _lock, urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError(f"plug {n}: not JSON: {body[:80]!r}") from None


def _power(r: dict, ch: int) -> str:
    v = r.get(f"POWER{ch}")
    if v is None and ch == 1:
        v = r.get("POWER")
    return str(v or "").upper()


def _round(v, nd: int):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _status_sync(n: int) -> dict:
    relay, lamp = _ch("PLUG_RELAY_CH", 1), _ch("PLUG_LAMP_CH", 2)
    socket = _power(_cmd(n, f"Power{relay}"), relay) == "ON"
    out = {"ok": True, "plug": n, "path": "lan", "online": True, "fw": "obk",
           "socket": socket}
    try:
        out["lamp"] = _power(_cmd(n, f"Power{lamp}"), lamp) == "ON"
    except Exception as exc:                          # noqa: BLE001
        log.info("plug %s lamp read failed: %s", n, exc)
    energy = ((_cmd(n, "STATUS 8").get("StatusSNS") or {}).get("ENERGY")) or {}
    out.update(volts=_round(energy.get("Voltage"), 1),
               kwh=_round(energy.get("ConsumptionTotal"), 3))
    watts = energy.get("Power")
    if watts is not None:
        min_w = float(os.environ.get("PLUG_MIN_WATTS", "5") or "5")
        watts = round(float(watts), 1)
        out.update(watts=watts, drawing=watts > min_w,
                   laptop_running=socket and watts > min_w)
    return out


def _switch_sync(n: int, on: bool) -> dict:
    relay = _ch("PLUG_RELAY_CH", 1)
    want = "ON" if on else "OFF"
    r = _cmd(n, f"Power{relay} {'On' if on else 'Off'}")
    if _power(r, relay) != want:
        raise RuntimeError(f"plug {n} answered {r}")
    return {"ok": True, "plug": n, "path": "lan", "state": want.lower()}


def _cycle_sync(n: int, gap: float) -> dict:
    _switch_sync(n, False)
    time.sleep(gap)
    _switch_sync(n, True)
    return {"ok": True, "plug": n, "path": "lan", "state": "on", "cycled": True}


async def run(op: str, n: int, gap: float = 10.0) -> dict:
    if n not in (1, 2):
        return {"ok": False, "error": "plug must be 1 or 2"}
    if not configured(n):
        return {"ok": False, "error": f"plug {n} not configured on this device"}
    try:
        if op == "status":
            return await asyncio.to_thread(_status_sync, n)
        if op in ("on", "off"):
            return await asyncio.to_thread(_switch_sync, n, op == "on")
        if op == "cycle":
            return await asyncio.to_thread(_cycle_sync, n, gap)
    except Exception as exc:                          # noqa: BLE001
        log.warning("plug %s %s failed: %s", n, op, exc)
        return {"ok": False, "plug": n, "path": "lan", "error": str(exc)}
    return {"ok": False, "error": "op must be status|on|off|cycle"}
