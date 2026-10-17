"""net_secondary.py — keep backup paths warm with periodic probes.

1) Wi‑Fi DSL: ensure associated + small TCP out that NIC (every ~10 min).
2) SMS Wi‑Fi (WIFI_SMS_SSID): while the SMS device sits charging / idle,
   briefly associate, send a touch, then restore DSL Wi‑Fi so the SMS
   sharing stays awake.

Does not permanently steer the default route away from the primary path,
except for the short SMS touch window.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Awaitable, Callable, Optional

import netctl
from config import Config
from winutil import powershell, run

log = logging.getLogger("net_secondary")

Notify = Callable[[str], Awaitable[None]]


def _reboot_busy() -> str:
    """Name of the box router_mgr is currently rebooting, or ""."""
    try:
        import router_mgr
        return router_mgr.reboot_in_progress()
    except Exception:                               # noqa: BLE001
        return ""


async def _notify(notify: Optional[Notify], msg: str) -> None:
    if not notify:
        return
    try:
        await notify(msg)
    except Exception:                                # noqa: BLE001
        pass


async def _alias_ipv4(alias: str) -> str:
    """IPv4 of one adapter by exact alias (incl. the USB NDIS tether)."""
    safe = (alias or "").replace("'", "''")
    if not safe:
        return ""
    ps = (
        f"$a = Get-NetAdapter -Name '{safe}' -ErrorAction SilentlyContinue; "
        "if (-not $a) { ''; exit } "
        "(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.*' } "
        "| Select-Object -First 1).IPAddress"
    )
    code, out = await powershell(ps, timeout=15)
    return (out or "").strip() if code == 0 else ""


async def _iface_ipv4(alias_hint: str) -> tuple[str, str]:
    """Return (InterfaceAlias, IPv4) for a Wi‑Fi or Ethernet adapter hint."""
    safe = alias_hint.replace("'", "''")
    ps = (
        f"$hints = @('{safe}','Wi-Fi','Wireless','WLAN','Ethernet'); "
        "$a = $null; "
        "foreach ($h in $hints) { "
        "  $a = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { "
        "    $_.Status -eq 'Up' -and ( "
        "      $_.Name -eq $h -or $_.Name -like ('*'+$h+'*') -or "
        "      $_.InterfaceDescription -like ('*'+$h+'*') "
        "    ) -and $_.InterfaceDescription -notmatch 'NDIS|Remote|Bluetooth' "
        "  } | Select-Object -First 1; "
        "  if ($a) { break } "
        "} "
        "if (-not $a) { '|'; exit } "
        "$ip = (Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.*' } "
        "| Select-Object -First 1).IPAddress; "
        "\"$($a.Name)|$ip\""
    )
    code, out = await powershell(ps, timeout=25)
    if code != 0 or not out or "|" not in out:
        return "", ""
    alias, _, ip = out.strip().partition("|")
    return alias.strip(), ip.strip()


def _tcp_via(local_ip: str, host: str, port: int, timeout: float) -> bool:
    """Small outbound TCP connect bound to local_ip (forces that NIC)."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.bind((local_ip, 0))
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:                       # noqa: BLE001
                pass


async def _enable_wifi() -> None:
    await powershell(
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Disabled' -and "
        "($_.Name -like '*Wi-Fi*' -or $_.Name -like '*Wireless*')} "
        "| Enable-NetAdapter -Confirm:$false",
        timeout=20,
    )


async def _wlan_connect(ssid: str) -> tuple[int, str]:
    await _enable_wifi()
    code, out = await run(
        f'netsh wlan connect name="{ssid}" ssid="{ssid}"', timeout=30)
    return code, out or ""


async def _tcp_probe_wifi(cfg: Config, label: str) -> dict:
    alias, ip = await _iface_ipv4("Wi-Fi")
    if not ip:
        return {"ok": False, "error": f"{label} has no IPv4", "alias": alias}
    host = cfg.net_check_host or "1.1.1.1"
    port = int(cfg.net_check_port or 53)
    ok = await asyncio.to_thread(
        _tcp_via, ip, host, port, float(cfg.net_check_timeout or 4))
    if not ok and port != 443:
        ok = await asyncio.to_thread(
            _tcp_via, ip, host, 443, float(cfg.net_check_timeout or 4))
    return {
        "ok": ok,
        "alias": alias,
        "ip": ip,
        "host": host,
        "port": port if ok else f"{port}/443",
        "error": None if ok else f"{label} TCP probe failed",
    }


async def probe_secondary(cfg: Config) -> dict:
    """Warm-check Wi‑Fi DSL without stealing SMS (or eth-only) routes.

    When not on DSL, uses forced WLAN API scan (same as SMS) so we don't
    false-fail or false-"OK" based on flaky netsh.
    """
    import net_pref
    import wlan_scan

    ssid = cfg.wifi_dsl_ssid
    if not ssid:
        return {"ok": False, "error": "WIFI_DSL_SSID not set"}

    pref = net_pref.get()
    cur_ssid, state = await netctl._wlan_ssid()  # noqa: SLF001
    sms = (cfg.wifi_sms_ssid or "").strip()
    on_sms = bool(sms and state == "connected" and cur_ssid == sms)
    on_dsl = state == "connected" and cur_ssid == ssid

    # The radio on the SMS hotspot is legitimate ONLY while sms is the active
    # egress (deliberate switch — manual pick or a rotation stint riding with
    # remember=False). If the radio sits on the hotspot but traffic leaves
    # elsewhere (parked there by an old drift), reclaim the DSL SSID so wifi
    # stays warm. Never reclaim while sms is actually carrying the traffic.
    if on_sms:
        active, _ip = await netctl.active_source(cfg)
        if (active or "").split(":")[0] != "sms":
            log.info("wifi radio parked on SMS hotspot while active path is "
                     "%s — reclaiming DSL SSID", active)
            on_sms = False

    # Not using DSL as active path — scan only (do not reconnect)
    if pref in ("sms", "eth") or on_sms:
        hit, names = await wlan_scan.ssid_visible(ssid, wait_s=4.0)
        if hit is True:
            return {
                "ok": True,
                "ssid": ssid,
                "mode": "scan",
                "visible": True,
                "scanned": len(names),
                "note": f"DSL on air ({len(names)} networks)",
                "skipped_connect": True,
            }
        if hit is None:
            return {
                "ok": True,
                "soft": True,
                "ssid": ssid,
                "mode": "scan",
                "scanned": len(names),
                "note": f"DSL scan inconclusive ({names[:5]})",
                "skipped_connect": True,
            }
        return {
            "ok": False,
            "ssid": ssid,
            "mode": "scan",
            "visible": False,
            "scanned": len(names),
            "error": f"main Wi‑Fi DSL not on air (saw {len(names)} networks)",
            "skipped_connect": True,
        }

    if not on_dsl:
        # Confirm SSID is on air before trying to connect
        hit, names = await wlan_scan.ssid_visible(ssid, wait_s=4.0)
        if hit is False:
            return {
                "ok": False,
                "ssid": ssid,
                "mode": "scan",
                "error": f"DSL not on air (saw {len(names)} networks) — skip connect",
                "scanned": len(names),
            }
        code, out = await _wlan_connect(ssid)
        if code != 0:
            return {"ok": False, "error": "wifi DSL connect failed", "detail": out}
        await asyncio.sleep(4)

    res = await _tcp_probe_wifi(cfg, "wifi DSL")
    res["ssid"] = ssid
    res["mode"] = "probe"
    return res


async def probe_sms_keepalive(cfg: Config) -> dict:
    """Check the SMS path without stealing the active route.

    Hotspot is the primary transport now (new topology):
    - preferred/active SMS: stay on the hotspot and TCP-probe; if preference
      is SMS but association was lost, reconnect and STAY.
    - otherwise: if the USB tether is plugged in, verify it (cheap, no radio);
      else scan-only — never associate-then-restore, that dance is what once
      stranded the DSL uplink.
    """
    import net_pref

    sms = (cfg.wifi_sms_ssid or "").strip()
    pref = net_pref.get()
    prev_ssid, prev_state = await netctl._wlan_ssid()  # noqa: SLF001
    on_sms = bool(sms and prev_state == "connected" and prev_ssid == sms)

    if not sms and not on_sms:
        # No hotspot configured — the tether is all there is to check.
        usb = await netctl.sms_usb_alias()
        if usb:
            ip = await _alias_ipv4(usb)
            ok = bool(ip)
            return {
                "ok": ok,
                "ssid": None,
                "alias": usb,
                "ip": ip,
                "mode": "usb",
                "transport": "usb",
                "restored": None,
                "note": (f"SMS box on USB tether ({usb} {ip})" if ok
                         else "USB tether up but no IPv4 yet"),
                "error": None if ok else "SMS USB tether has no IPv4",
            }
        return {"ok": False, "error": "no USB tether and WIFI_SMS_SSID not set",
                "skipped": True}

    if pref == "sms" or on_sms:
        if not on_sms:
            code, out = await _wlan_connect(sms)
            if code != 0:
                return {
                    "ok": False,
                    "error": "SMS preferred but connect failed",
                    "detail": out,
                    "ssid": sms,
                }
            await asyncio.sleep(5)
            await netctl._set_preference(cfg, "sms")  # noqa: SLF001
        res = await _tcp_probe_wifi(cfg, "SMS Wi-Fi")
        res["ssid"] = sms
        res["mode"] = "stay"
        res["restored"] = sms
        return res

    # Preferred path is not SMS. If the USB tether is plugged in, verifying it
    # is free (no radio involved) and proves the box is alive.
    usb = await netctl.sms_usb_alias()
    if usb:
        ip = await _alias_ipv4(usb)
        if ip:
            return {
                "ok": True,
                "ssid": sms,
                "alias": usb,
                "ip": ip,
                "mode": "usb",
                "transport": "usb",
                "restored": None,
                "note": f"SMS box alive on USB tether ({usb} {ip})",
                "error": None,
            }

    # No tether (or tether has no IP) — forced WLAN API scan of the hotspot.
    import wlan_scan

    has_profile = await netctl._wlan_profile_exists(sms)  # noqa: SLF001
    hit, names = await wlan_scan.ssid_visible(sms, wait_s=4.0)
    if hit is True:
        return {
            "ok": True,
            "ssid": sms,
            "mode": "scan",
            "visible": True,
            "profile": has_profile,
            "scanned": len(names),
            "restored": None,
            "error": None,
            "note": f"SSID on air ({len(names)} networks seen)",
        }
    if hit is None:
        # Inconclusive (scan only saw current AP) — not a TG alarm
        return {
            "ok": True,
            "ssid": sms,
            "mode": "scan",
            "soft": True,
            "visible": False,
            "profile": has_profile,
            "scanned": len(names),
            "restored": None,
            "error": None,
            "note": (
                f"scan inconclusive ({len(names)} nets: {names[:5]}); "
                "profile ok" if has_profile else "scan inconclusive, no profile"
            ),
        }
    # Real scan saw other networks but not SMS → hotspot likely off
    return {
        "ok": False,
        "ssid": sms,
        "mode": "scan",
        "visible": False,
        "profile": has_profile,
        "scanned": len(names),
        "restored": None,
        "error": (
            f"SMS hotspot not on air (saw {len(names)} networks, "
            f"not {sms})"
        ),
    }


class SecondaryNetWatch:
    def __init__(self, cfg: Config, notify: Optional[Notify] = None) -> None:
        self._cfg = cfg
        self._notify = notify
        self._last_wifi_ok: bool | None = None
        self._last_sms_ok: bool | None = None
        self._last_result: dict = {}

    @property
    def last_result(self) -> dict:
        return dict(self._last_result)

    async def run(self) -> None:
        wifi_on = getattr(self._cfg, "net_second_check_enabled", True)
        sms_on = getattr(self._cfg, "sms_keepalive_enabled", True)
        if not wifi_on and not sms_on:
            log.info("secondary net / sms keepalive both disabled")
            return

        interval = float(getattr(self._cfg, "net_second_check_interval", 600) or 600)
        sms_interval = float(
            getattr(self._cfg, "sms_keepalive_interval", 180) or 180)
        log.info(
            "warm-path watch started (wifi_dsl every %.0fs=%s, sms wifi every %.0fs=%s)",
            interval, wifi_on, sms_interval, sms_on,
        )
        await asyncio.sleep(25)

        next_wifi = time.time()
        next_sms = time.time() + 45

        while True:
            now = time.time()
            try:
                # A warm check hops onto the other SSID for a few seconds. While
                # router_mgr is rebooting a box that would fight the interlock
                # and raise false "Wi‑Fi check FAILED" alerts, so stand down and
                # push both probes past the outage window.
                busy = _reboot_busy()
                if busy:
                    log.debug("warm-path tick skipped — %s reboot running", busy)
                    next_wifi = max(next_wifi, time.time() + 60)
                    next_sms = max(next_sms, time.time() + 60)
                    await asyncio.sleep(5)
                    continue
                if wifi_on and now >= next_wifi:
                    await self._tick_wifi()
                    next_wifi = time.time() + interval
                if sms_on and now >= next_sms:
                    await self._tick_sms()
                    next_sms = time.time() + sms_interval
            except Exception as exc:                 # noqa: BLE001
                log.warning("warm-path tick error: %s", exc)
                await _notify(self._notify, f"⚠ Warm-path check error: {exc}")
            await asyncio.sleep(5)

    async def _tick_wifi(self) -> None:
        eth = "absent"
        if self._cfg.eth_adapter:
            eth = await netctl.adapter_status(self._cfg.eth_adapter)

        t0 = time.time()
        res = await probe_secondary(self._cfg)
        res["eth"] = eth
        res["took_s"] = round(time.time() - t0, 1)
        self._last_result["wifi"] = res

        ok = bool(res.get("ok"))
        soft = bool(res.get("soft"))
        if ok:
            log.info(
                "wifi DSL warm OK (mode=%s ssid=%s ip=%s note=%s took=%ss)",
                res.get("mode"), res.get("ssid"), res.get("ip"),
                res.get("note") or res.get("reason"), res.get("took_s"),
            )
            if self._last_wifi_ok is False and not soft:
                await _notify(
                    self._notify,
                    f"Main Wi‑Fi (DSL) recovered ({res.get('ip') or res.get('ssid')})",
                )
        else:
            msg = (
                f"⚠ Main Wi‑Fi (DSL) check FAILED "
                f"({res.get('error') or res}) eth={eth}"
            )
            log.warning(msg)
            if self._last_wifi_ok is not False:
                await _notify(self._notify, msg)
        self._last_wifi_ok = ok

    async def _tick_sms(self) -> None:
        if not (self._cfg.wifi_sms_ssid or "").strip():
            log.debug("sms keepalive skipped — no WIFI_SMS_SSID")
            return

        t0 = time.time()
        res = await probe_sms_keepalive(self._cfg)
        res["took_s"] = round(time.time() - t0, 1)
        self._last_result["sms"] = res
        if res.get("skipped"):
            return

        ok = bool(res.get("ok"))
        soft = bool(res.get("soft"))
        if ok:
            log.info(
                "sms check OK (mode=%s ssid=%s ip=%s note=%s took=%ss)",
                res.get("mode"), res.get("ssid"), res.get("ip"),
                res.get("note"), res.get("took_s"),
            )
            if self._last_sms_ok is False and not soft:
                await _notify(
                    self._notify,
                    f"SMS backup recovered ({res.get('ip') or res.get('ssid')})",
                )
        else:
            # Real failure only in stay mode (user on / prefers SMS)
            msg = (
                f"⚠ SMS backup FAILED "
                f"({res.get('error') or res})"
            )
            log.warning(msg)
            if self._last_sms_ok is not False:
                await _notify(self._notify, msg)
        self._last_sms_ok = ok
