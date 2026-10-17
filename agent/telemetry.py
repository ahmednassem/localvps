"""telemetry.py — build the periodic status payload sent on the control tunnel.

Combines psutil counters, eth + Wi‑Fi status (netctl) and CPU/GPU temperature
(hwtemp). Designed to never raise: any sub-probe that fails contributes null.
(Process/unit states are gone — process control lives with the local services.)
"""
from __future__ import annotations

import asyncio
import logging

import psutil

import anydesk_mgr
import cluster_local
import hwtemp
import netctl
import peer
import plugs
from config import Config
from version import VERSION

log = logging.getLogger("telemetry")

# prime cpu_percent so the first real read isn't 0.0
try:
    psutil.cpu_percent(interval=None)
except Exception:                                   # noqa: BLE001
    pass


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:                        # noqa: BLE001
        log.debug("telemetry probe failed: %s", exc)
        return default


HOME_PROBE_TIMEOUT_S = 12.0


async def _home_view(cfg: Config) -> dict:
    """What this device sees on the home LAN: the DSL router and both plugs."""
    router_ok = await netctl.has_internet(cfg.router_dsl_host, 80, timeout=1.5)
    view: dict = {"router_ok": router_ok, "plugs": {}}
    wanted = [n for n in (1, 2) if plugs.configured(n)]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(plugs.run("status", n) for n in wanted)),
            HOME_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        results = [{"ok": False, "error": "timeout"} for _ in wanted]
    for n, r in zip(wanted, results):
        view["plugs"][str(n)] = {k: r.get(k) for k in ("ok", "socket", "watts", "error")
                                 if r.get(k) is not None}
    return view


async def collect(cfg: Config) -> dict:
    import device_registry
    device_registry.apply_to_config(cfg)
    cpu = _safe(lambda: psutil.cpu_percent(interval=None))
    ram = _safe(lambda: psutil.virtual_memory().percent)
    disk = _safe(lambda: psutil.disk_usage("C:\\").percent)

    battery = None
    plugged = None
    charging = None
    battery_secs = None
    batt = _safe(lambda: psutil.sensors_battery())
    if batt is not None:
        battery = round(batt.percent)
        plugged = bool(batt.power_plugged)
        if not plugged:
            charging = False
        elif battery < 99:
            charging = True
        else:
            charging = False
        if batt.secsleft not in (None, psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            battery_secs = int(batt.secsleft)

    temp = _safe(hwtemp.read_temp_c)

    try:
        source, ip = await netctl.active_source(cfg)
    except Exception as exc:                         # noqa: BLE001
        log.debug("net probe failed: %s", exc)
        source, ip = "unknown", ""

    # Networks we care about: cable eth + Wi‑Fi (DSL). No fan / SMS USB ports.
    dev = {}
    if cfg.eth_adapter:
        try:
            dev["eth"] = await netctl.adapter_status(cfg.eth_adapter)
        except Exception as exc:                     # noqa: BLE001
            log.debug("eth status failed: %s", exc)
            dev["eth"] = "unknown"
    try:
        ssid, state = await netctl._wlan_ssid()  # noqa: SLF001
        # Same path_status rules (ready = saved profile → selectable)
        if cfg.wifi_dsl_ssid:
            dev["wifi"] = await netctl.path_status(cfg, "wifi")
        else:
            dev["wifi"] = "absent"
        if cfg.wifi_sms_ssid:
            dev["sms"] = await netctl.path_status(cfg, "sms")
        else:
            dev["sms"] = "absent"
    except Exception as exc:                         # noqa: BLE001
        log.debug("wifi status failed: %s", exc)
        dev["wifi"] = "unknown"
        dev["sms"] = "unknown"

    try:
        online = await netctl.has_internet(cfg.net_check_host, cfg.net_check_port,
                                           timeout=cfg.net_check_timeout)
    except Exception:                                # noqa: BLE001
        online = None

    try:
        home = await _home_view(cfg)
    except Exception as exc:                         # noqa: BLE001
        log.debug("home view failed: %s", exc)
        home = {}

    ad = _safe(lambda: {
        "running": anydesk_mgr.is_running(),
        "elevated": anydesk_mgr.is_elevated() if anydesk_mgr.is_running() else False,
        "id": anydesk_mgr.read_id(cfg),
    }, {})

    return {
        "type": "telemetry",
        "net": source,
        "ip": ip,
        "internet": online,
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "temp": temp,
        # Always include power fields on every status tick
        "battery": battery,
        "battery_pct": battery,
        "plugged": plugged,
        "ac_connected": plugged,
        "charging": charging,
        "battery_secs": battery_secs,
        "power": (
            f"AC ({battery}%)" if plugged and battery is not None
            else f"BATTERY ({battery}%)" if battery is not None
            else "unknown"
        ),
        "devices": dev,
        "home": home,
        "peer": _safe(peer.snapshot, {}),
        "lead": _safe(cluster_local.snapshot, {}),
        "version": VERSION,
        "anydesk": ad,
    }
