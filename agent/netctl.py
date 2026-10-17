"""netctl.py — detect, test and switch the active internet source.

Three sources, three separate WANs (new topology):
  wifi  the main DSL router's Wi-Fi (radio)
  eth   the NEW router over the Ethernet cable — its own WAN, not the DSL box
  sms   the SMS box over its Wi-Fi hotspot (primary transport);
        the USB Remote NDIS tether is only a fallback when the hotspot is off

The laptop has ONE Wi-Fi radio shared by wifi and sms, so a wifi<->sms switch
is a re-association (~10 s). Only eth is guaranteed up during it. Windows must
never roam between the two SSIDs on its own: the SMS hotspot profile is kept
connectionmode=manual and only netctl.switch() moves the radio.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket

from config import Config
from winutil import powershell, run

log = logging.getLogger("netctl")

FAILOVER_ORDER = ("wifi", "eth", "sms")


async def _wlan_ssid() -> tuple[str, str]:
    code, out = await run("netsh wlan show interfaces", timeout=15)
    if code != 0 or not out:
        return "", ""
    ssid = ""
    state = ""
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^SSID\s*:\s*(.+)$", line)
        if m and "BSSID" not in line:
            ssid = m.group(1).strip()
        m = re.match(r"^State\s*:\s*(.+)$", line)
        if m:
            state = m.group(1).strip().lower()
    return ssid, state


async def _wlan_ssid_visible(want: str) -> bool:
    """True if SSID appears after a forced Native Wi‑Fi scan.

    Prefer wlanapi (real scan). Fall back to netsh. Inconclusive scans
    (only current SSID listed) return False here — callers that need
    soft/inconclusive should use wlan_scan.ssid_visible().
    """
    want = (want or "").strip()
    if not want:
        return False
    try:
        import wlan_scan
        hit, _names = await wlan_scan.ssid_visible(want, wait_s=4.0)
        if hit is True:
            return True
        if hit is False:
            return False
        # inconclusive → fall through to netsh
    except Exception as exc:                         # noqa: BLE001
        log.debug("wlanapi scan unavailable: %s", exc)

    code, out = await run("netsh wlan show networks mode=bssid", timeout=25)
    if code != 0 or not out:
        return False
    want_l = want.lower()
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line, re.I)
        if not m:
            m = re.match(r"^SSID\s*:\s*(.+)$", line, re.I)
            if m and "BSSID" in line.upper():
                continue
        if not m:
            continue
        name = (m.group(1) or "").strip()
        if name.lower() == want_l:
            return True
    return False


async def _wlan_profile_exists(want: str) -> bool:
    """True if a saved WLAN profile for this SSID exists (selectable)."""
    want = (want or "").strip()
    if not want:
        return False
    code, out = await run("netsh wlan show profiles", timeout=20)
    if code != 0 or not out:
        return False
    want_l = want.lower()
    for line in out.splitlines():
        # "All User Profile     : MY-4G-BOX"
        if ":" not in line:
            continue
        name = line.split(":", 1)[1].strip()
        if name.lower() == want_l:
            return True
    return False


async def sms_usb_alias() -> str:
    """Alias of the SMS box's USB Remote NDIS adapter if it is Up, else ""."""
    ps = (
        "$a = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { "
        "($_.InterfaceDescription -match 'Remote NDIS|NDIS Compatible' "
        "-or $_.Name -eq 'Ethernet 2') -and $_.Status -eq 'Up' } "
        "| Select-Object -First 1; "
        "if ($a) { $a.Name } else { '' }"
    )
    code, out = await powershell(ps, timeout=15)
    if code != 0:
        return ""
    return (out or "").strip()


def _is_sms_tether_alias(alias: str) -> bool:
    """USB Remote NDIS / Ethernet 2 — SMS box tether, NOT cable ethernet."""
    a = (alias or "").lower().strip()
    if not a:
        return False
    if a == "ethernet 2":
        return True
    if "ndis" in a or "remote" in a:
        return True
    return False


async def _default_iface() -> tuple[str, str]:
    # Prefer lowest InterfaceMetric, then RouteMetric (both matter on Windows).
    # Never pick USB Remote NDIS over Wi‑Fi when metrics tie incorrectly.
    ps = (
        "$routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
        "-ErrorAction SilentlyContinue; "
        "if (-not $routes) { '|' ; exit } "
        "$best = $null; $bestScore = [int]::MaxValue; "
        "foreach ($r in @($routes)) { "
        "  $a = Get-NetAdapter -InterfaceIndex $r.ifIndex -ErrorAction SilentlyContinue; "
        "  if ($null -eq $a -or $a.Status -ne 'Up') { continue } "
        "  $ifaceMet = 50; "
        "  try { "
        "    $ipif = Get-NetIPInterface -InterfaceIndex $r.ifIndex "
        "      -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
        "    if ($ipif) { $ifaceMet = [int]$ipif.InterfaceMetric } "
        "  } catch {} "
        "  $score = ([int]$ifaceMet) + ([int]$r.RouteMetric); "
        "  $isNdis = ($a.InterfaceDescription -match 'NDIS|Remote' "
        "             -or $a.Name -eq 'Ethernet 2'); "
        # Penalise the USB SMS tether so it cannot win by accident — but not
        # when it has been deliberately boosted (metric <= 10 is only ever set
        # by switch(cfg,'sms') over USB), otherwise we would report wifi while
        # traffic actually leaves through the SMS box.
        "  if ($isNdis -and $ifaceMet -gt 10) { $score += 1000 } "
        "  if ($score -lt $bestScore) { $bestScore = $score; $best = $r } "
        "}; "
        "if ($null -eq $best) { '|' ; exit } "
        "$ip = (Get-NetIPAddress -InterfaceIndex $best.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress; "
        "$al = (Get-NetAdapter -InterfaceIndex $best.ifIndex "
        "-ErrorAction SilentlyContinue).Name; "
        "\"$al|$ip\""
    )
    code, out = await powershell(ps, timeout=20)
    if code != 0 or not out or "|" not in out:
        return "", ""
    alias, _, ip = out.strip().partition("|")
    return alias.strip(), ip.strip()


async def active_source(cfg: Config) -> tuple[str, str]:
    """Return (wifi|eth|sms|…, ip). Cable eth only — never USB NDIS as eth.

    Order matters: default *route* first. If cable Ethernet owns the default
    route, report eth even while Wi‑Fi is still associated to DSL (otherwise
    picking Ethernet in the UI snaps the radio back to wifi).
    """
    alias, ip = await _default_iface()
    ssid, state = await _wlan_ssid()
    alias_l = (alias or "").lower()
    is_wireless = (
        "wi-fi" in alias_l or "wireless" in alias_l or "wlan" in alias_l
    )
    eth_name = (cfg.eth_adapter or "Ethernet").strip().lower()

    # 1) USB NDIS tether — never "eth". If the tether owns the default route
    # then traffic leaves through the SMS box, whatever the radio is associated
    # to, so report sms. _default_iface only lets the tether win when it was
    # deliberately preferred (or nothing else is up).
    if _is_sms_tether_alias(alias):
        return "sms", ip

    # 2) Cable Ethernet owns default route → eth
    if alias and not is_wireless:
        if alias_l in (eth_name, "ethernet") or (
                eth_name and alias_l == eth_name):
            if await adapter_status(cfg.eth_adapter or "Ethernet") == "on":
                return "eth", ip

    # 3) Wi‑Fi association (wifi / sms hotspot)
    if state == "connected" and ssid:
        if cfg.wifi_sms_ssid and ssid == cfg.wifi_sms_ssid:
            return "sms", ip
        if cfg.wifi_dsl_ssid and ssid == cfg.wifi_dsl_ssid:
            return "wifi", ip
        if is_wireless or not alias:
            return f"wifi:{ssid}", ip

    if alias and not is_wireless and not _is_sms_tether_alias(alias):
        return alias, ip

    if ssid:
        label = "sms" if ssid == cfg.wifi_sms_ssid else "wifi"
        return label, ip or ""
    return "none", ""


async def has_internet(host: str = "1.1.1.1", port: int = 53,
                       timeout: float = 4.0) -> bool:
    """True if we can open a TCP connection (real reachability, not just link up)."""

    def _probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return await asyncio.to_thread(_probe)


def configured_sources(cfg: Config) -> list[str]:
    out = []
    if cfg.wifi_dsl_ssid:
        out.append("wifi")
    if cfg.eth_adapter:
        out.append("eth")
    if cfg.wifi_sms_ssid:
        out.append("sms")
    return out


def next_failover(cfg: Config, current: str) -> str | None:
    """Next source in wifi → eth → sms rotation (skips unconfigured)."""
    avail = configured_sources(cfg)
    if not avail:
        return None
    order = [s for s in FAILOVER_ORDER if s in avail]
    if not order:
        return None
    cur = current.split(":")[0] if current else ""
    if cur not in order:
        return order[0]
    idx = order.index(cur)
    if idx + 1 < len(order):
        return order[idx + 1]
    return order[0]  # wrap to first


async def adapter_status(name: str) -> str:
    """Cable ethernet link status only (rejects SMS Remote NDIS)."""
    if not name:
        return "absent"
    # Never treat SMS tether NIC as cable eth
    low = name.lower()
    if "ndis" in low or "remote" in low or name.strip().lower() == "ethernet 2":
        return "absent"
    safe = name.replace("'", "''")
    ps = (
        f"$a = Get-NetAdapter -Name '{safe}' -ErrorAction SilentlyContinue; "
        "if ($null -eq $a) { "
        f"  $a = Get-NetAdapter -InterfaceDescription '{safe}' "
        "-ErrorAction SilentlyContinue "
        "}; "
        "if ($null -eq $a) { 'absent' } "
        "elseif ($a.InterfaceDescription -match 'NDIS|Remote NDIS') { 'absent' } "
        "elseif ($a.Status -eq 'Up') { 'on' } else { 'off' }"
    )
    code, out = await powershell(ps, timeout=15)
    out = (out or "").strip().lower()
    if out in ("on", "off", "absent"):
        return out
    return "unknown"


async def _ssid_on_air(want: str, wait_s: float = 3.5) -> tuple[bool | None, list[str]]:
    """Forced WLAN scan for one SSID. True / False / None (inconclusive)."""
    want = (want or "").strip()
    if not want:
        return False, []
    try:
        import wlan_scan
        return await wlan_scan.ssid_visible(want, wait_s=wait_s)
    except Exception as exc:                         # noqa: BLE001
        log.debug("ssid on-air scan failed: %s", exc)
        visible = await _wlan_ssid_visible(want)
        return (True if visible else None), []


async def path_status(cfg: Config, source: str) -> str:
    """Live status for eth|wifi|sms — same meanings as telemetry devices.*."""
    source = (source or "").lower().strip()
    if source in ("eth", "ethernet"):
        if not cfg.eth_adapter:
            return "absent"
        return await adapter_status(cfg.eth_adapter)

    ssid, state = await _wlan_ssid()
    if source == "wifi":
        if not cfg.wifi_dsl_ssid:
            return "absent"
        if state == "connected" and ssid == cfg.wifi_dsl_ssid:
            return "on"
        if state == "connected" and ssid and (
                not cfg.wifi_sms_ssid or ssid != cfg.wifi_sms_ssid):
            return f"on:{ssid}"
        # Same forced scan as SMS — avoid false standby/off from netsh
        hit, _ = await _ssid_on_air(cfg.wifi_dsl_ssid)
        if hit is True:
            return "on"
        if hit is False:
            return "off"
        if await _wlan_profile_exists(cfg.wifi_dsl_ssid):
            return "standby"
        return "off"

    if source == "sms":
        # Primary transport is the box's Wi-Fi hotspot; the USB tether is the
        # fallback. Either one being reachable makes the path usable.
        hit = None
        if cfg.wifi_sms_ssid:
            if state == "connected" and ssid == cfg.wifi_sms_ssid:
                return "on"
            hit, _ = await _ssid_on_air(cfg.wifi_sms_ssid)
            if hit is True:
                return "on"
        if await sms_usb_alias():
            return "on"
        if not cfg.wifi_sms_ssid:
            return "absent"
        if hit is False:
            return "off"
        if await _wlan_profile_exists(cfg.wifi_sms_ssid):
            return "standby"
        return "off"

    return "unknown"


def path_is_up(status: str) -> bool:
    """True if the path can be selected / used (on, or standby=saved profile)."""
    s = (status or "").lower().strip()
    return s in ("on", "standby", "ready") or s.startswith("on:")


async def ensure_path_up(cfg: Config, source: str) -> dict | None:
    """If source is not currently up, return an error dict; else None."""
    source = (source or "").lower().strip()
    if source in ("ethernet",):
        source = "eth"
    if source not in ("eth", "wifi", "sms"):
        return {"ok": False, "error": "source must be eth|wifi|sms"}
    status = await path_status(cfg, source)
    if path_is_up(status):
        return None
    label = {
        "eth": "Ethernet",
        "wifi": "Wi-Fi DSL",
        "sms": "SMS Wi-Fi",
    }[source]
    return {
        "ok": False,
        "error": f"{label} is not up (status={status}) — cannot switch",
        "source": source,
        "status": status,
    }


async def _set_preference(cfg: Config, preferred: str) -> None:
    """Make preferred iface win the default route (lower metric = preferred)."""
    preferred = preferred.lower()
    eth_name = (cfg.eth_adapter or "Ethernet").replace("'", "''")
    # Ethernet 2 is the SMS Remote NDIS — never treat it as cable eth.
    ps = f"$ethName = '{eth_name}'\n" + r"""
$wifi = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match 'Wi-Fi|Wireless|WLAN' -and $_.InterfaceDescription -notmatch 'NDIS|Remote'
}
$eth = Get-NetAdapter -Name $ethName -ErrorAction SilentlyContinue
if ($null -eq $eth) {
  $eth = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq $ethName -or $_.Name -eq 'Ethernet' -or (
      $_.InterfaceDescription -match 'Realtek|Intel|Broadcom|Killer|GbE' -and
      $_.InterfaceDescription -notmatch 'NDIS|Remote|Wi-Fi|Wireless' -and
      $_.Name -notmatch 'Wi-Fi|Wireless'
    )
  } | Select-Object -First 1
}
$sms = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
  $_.InterfaceDescription -match 'Remote NDIS|NDIS Compatible' -or $_.Name -eq 'Ethernet 2'
}
function Set-M($adapters, $metric) {
  foreach ($a in @($adapters)) {
    if ($null -eq $a) { continue }
    try {
      Set-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4 `
        -InterfaceMetric $metric -ErrorAction Stop | Out-Null
      Write-Output ("metric:$($a.Name)=$metric")
    } catch {
      Write-Output ("metric_fail:$($a.Name):$($_.Exception.Message)")
    }
  }
}
# Windows picks the default route on InterfaceMetric + RouteMetric, so setting
# only the interface metric is not enough: the USB tether ships with
# RouteMetric 80 and would still lose to Wi-Fi's 5 however low we push its
# interface metric.
function Set-R($adapters, $metric) {
  foreach ($a in @($adapters)) {
    if ($null -eq $a) { continue }
    try {
      Get-NetRoute -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $a.ifIndex `
        -AddressFamily IPv4 -ErrorAction Stop |
        Set-NetRoute -RouteMetric $metric -ErrorAction Stop | Out-Null
      Write-Output ("route:$($a.Name)=$metric")
    } catch {
      Write-Output ("route_fail:$($a.Name):$($_.Exception.Message)")
    }
  }
}
# baseline: wifi preferred, then sms, then eth
Set-M $wifi 15
Set-M $sms 35
Set-M $eth 50
"""
    if preferred == "wifi":
        # DSL via Wi‑Fi; demote USB NDIS so it never steals "active=eth"
        ps += ("\nSet-M $wifi 5\nSet-M $eth 50\nSet-M $sms 80\n"
               "Set-R $wifi 0\nSet-R $eth 50\nSet-R $sms 80\n")
    elif preferred == "eth":
        ps += ("\nSet-M $eth 5\nSet-M $wifi 50\nSet-M $sms 80\n"
               "Set-R $eth 0\nSet-R $wifi 60\nSet-R $sms 80\n")
    elif preferred == "sms":
        # SMS over the Wi‑Fi hotspot: the radio itself carries SMS traffic, so
        # Wi‑Fi stays primary and USB NDIS is demoted (boosting it here used to
        # cause net=eth while cable Ethernet was Disconnected).
        ps += ("\nSet-M $wifi 5\nSet-M $sms 80\nSet-M $eth 50\n"
               "Set-R $wifi 0\nSet-R $sms 80\nSet-R $eth 50\n")
    elif preferred == "smsusb":
        # SMS over the USB tether: this is the only case where NDIS is allowed
        # to win the default route. Keep it <= 10 so _default_iface treats the
        # win as deliberate instead of accidental.
        ps += ("\nSet-M $sms 5\nSet-M $wifi 50\nSet-M $eth 60\n"
               "Set-R $sms 0\nSet-R $wifi 60\nSet-R $eth 70\n")
    await powershell(ps, timeout=25)


async def _demote_sms_profile(cfg: Config) -> None:
    """Stop WINDOWS from auto-joining the SMS hotspot on its own.

    The hotspot is a first-class transport again (switch("sms") associates to
    it deliberately), but the profile stays connectionmode=manual: the strong,
    saved 4G box signal once auto-captured the radio and stranded the DSL uplink
    (2026-09-11 bug). Manual profiles still connect fine via netsh — only the
    agent moves the radio, never Windows roaming.
    """
    sms = (cfg.wifi_sms_ssid or "").strip()
    if sms:
        await run(
            f'netsh wlan set profileparameter name="{sms}" '
            'connectionmode=manual',
            timeout=15,
        )
    dsl = (cfg.wifi_dsl_ssid or "").strip()
    if dsl:
        await run(
            f'netsh wlan set profileorder name="{dsl}" '
            'interface="Wi-Fi" priority=1',
            timeout=15,
        )


async def prefer_wifi(cfg: Config, *, remember: bool = True) -> dict:
    """Connect DSL Wi-Fi and make it the default route (startup preference)."""
    if not cfg.wifi_dsl_ssid:
        return {"ok": False, "error": "WIFI_DSL_SSID not configured"}
    res = await switch(cfg, "wifi", remember=remember)
    await _set_preference(cfg, "wifi")
    return res


async def _route_matches(cfg: Config, want: str) -> tuple[bool, str, str]:
    """True if active_source reports the requested path."""
    want = (want or "").split(":")[0].lower()
    src, ip = await active_source(cfg)
    got = (src or "").split(":")[0].lower()
    return got == want, src, ip


async def _switch_sms_usb(cfg: Config, usb: str, *, remember: bool) -> dict:
    """SMS over the USB tether — the fallback transport when the hotspot is off."""
    import net_pref
    await _set_preference(cfg, "smsusb")
    await asyncio.sleep(1.5)
    ok, src, ip = await _route_matches(cfg, "sms")
    if not ok:
        await _set_preference(cfg, "smsusb")
        await asyncio.sleep(1.5)
        ok, src, ip = await _route_matches(cfg, "sms")
    if ok and remember:
        net_pref.set_source("sms")
    return {
        "ok": ok,
        "output": (f"SMS box via USB tether ({usb})" if ok
                   else f"route still on {src} ({ip})"),
        "code": 0,
        "source": "sms",
        "transport": "usb",
        "active": src,
        "ip": ip,
    }


async def switch(cfg: Config, source: str, *, remember: bool = True) -> dict:
    """Switch the active internet source to eth | wifi | sms.

    `remember=False` moves the route without touching the sticky preference in
    net_pref. Automatic movers (watchdog failover, router-reboot interlock) must
    pass it, otherwise a temporary hop away from the chosen path rewrites the
    user's choice and net_startup then re-asserts the wrong path on every
    startup / wake — the long-standing "SMS keeps reverting to Wi‑Fi" bug.
    """
    import net_pref

    source = (source or "").lower().strip()

    if source == "sms":
        # Primary transport: the box's Wi-Fi hotspot (re-associate the radio).
        # Fall back to the USB tether only when the hotspot is not usable —
        # not connected, not on the air — or the association fails below.
        hotspot_usable = False
        if cfg.wifi_sms_ssid:
            ssid_now, state_now = await _wlan_ssid()
            if state_now == "connected" and ssid_now == cfg.wifi_sms_ssid:
                hotspot_usable = True
            else:
                hit, _ = await _ssid_on_air(cfg.wifi_sms_ssid)
                hotspot_usable = hit is not False   # True or inconclusive
        if not hotspot_usable:
            usb = await sms_usb_alias()
            if not usb:
                return {"ok": False, "source": "sms",
                        "error": "SMS box unreachable: hotspot not on the air "
                                 "and no USB tether"}
            return await _switch_sms_usb(cfg, usb, remember=remember)

    if source in ("wifi", "sms"):
        ssid = cfg.wifi_dsl_ssid if source == "wifi" else cfg.wifi_sms_ssid
        if not ssid:
            return {"ok": False, "error": f"{source} SSID not configured"}
        # Idempotent fast path: if the radio is already associated to the
        # target SSID, do NOT re-run `netsh wlan connect`. That command tears
        # the association down and brings it back up, which drops every live
        # session on the link (RustDesk/AnyDesk cut out). Startup/wake/login
        # bring-ups fire this repeatedly, so re-asserting the route must be
        # cheap — only nudge the interface metrics via _set_preference.
        ssid_now, state_now = await _wlan_ssid()
        if state_now == "connected" and ssid_now == ssid:
            if source == "wifi":
                await _demote_sms_profile(cfg)
            await _set_preference(cfg, source)
            ok, src, ip = await _route_matches(cfg, source)
            if not ok:
                await _set_preference(cfg, source)
                await asyncio.sleep(1.0)
                ok, src, ip = await _route_matches(cfg, source)
            if ok and remember:
                net_pref.set_source(source)
            return {
                "ok": ok,
                "output": "already associated" if ok else f"route on {src} ({ip})",
                "code": 0,
                "source": source,
                "transport": "hotspot" if source == "sms" else None,
                "active": src,
                "ip": ip,
                "reassociated": False,
            }
        await powershell(
            "Get-NetAdapter | Where-Object {$_.Status -eq 'Disabled' -and "
            "($_.Name -like '*Wi-Fi*' -or $_.Name -like '*Wireless*')} "
            "| Enable-NetAdapter -Confirm:$false",
            timeout=20,
        )
        # Prefer this SSID so Windows won't auto-jump back to the other profile
        await run(
            f'netsh wlan set profileorder name="{ssid}" interface="Wi-Fi" priority=1',
            timeout=15,
        )
        code, out = await run(
            f'netsh wlan connect name="{ssid}" ssid="{ssid}" interface="Wi-Fi"',
            timeout=30,
        )
        if code != 0:
            code, out = await run(
                f'netsh wlan connect name="{ssid}" ssid="{ssid}"', timeout=30)
        if code != 0:
            if source == "sms":
                # hotspot association failed — try the USB tether before
                # giving up on the SMS path entirely
                usb = await sms_usb_alias()
                if usb:
                    log.warning("SMS hotspot connect failed (%s) — "
                                "falling back to USB tether", out)
                    return await _switch_sms_usb(cfg, usb, remember=remember)
            return {"ok": False, "output": out, "code": code, "source": source}
        if source == "wifi":
            # keep the radio from auto-joining the SMS hotspot on its own
            await _demote_sms_profile(cfg)
        await _set_preference(cfg, source)
        await asyncio.sleep(1.5)
        ok, src, ip = await _route_matches(cfg, source)
        if not ok:
            await _set_preference(cfg, source)
            await asyncio.sleep(1.0)
            ok, src, ip = await _route_matches(cfg, source)
        if ok and remember:
            net_pref.set_source(source)
        return {
            "ok": ok,
            "output": out if ok else f"route still on {src} ({ip})",
            "code": code,
            "source": source,
            "transport": "hotspot" if source == "sms" else None,
            "active": src,
            "ip": ip,
        }
    if source in ("eth", "ethernet"):
        if not cfg.eth_adapter:
            return {"ok": False, "error": "ETH_ADAPTER not configured"}
        safe = cfg.eth_adapter.replace("'", "''")
        ps = (
            f"$a = Get-NetAdapter -Name '{safe}' -ErrorAction SilentlyContinue; "
            f"if ($null -eq $a) {{ $a = Get-NetAdapter -InterfaceDescription '{safe}' "
            "-ErrorAction SilentlyContinue }; "
            "if ($null -eq $a) { Write-Error 'adapter not found'; exit 1 }; "
            "if ($a.Status -ne 'Up') { Write-Error 'ethernet link down'; exit 2 }; "
            "Enable-NetAdapter -Name $a.Name -Confirm:$false; "
            "Write-Output $a.Name"
        )
        code, out = await powershell(ps, timeout=30)
        if code != 0:
            return {"ok": False, "output": out, "code": code, "source": "eth"}
        await _set_preference(cfg, "eth")
        # eth is its own WAN (the new router) — keep the radio associated to
        # the DSL SSID so wifi stays warm and a later switch is instant. Only
        # reclaim the radio if it drifted onto the SMS hotspot.
        ssid_now, state_now = await _wlan_ssid()
        if (cfg.wifi_dsl_ssid and state_now == "connected"
                and cfg.wifi_sms_ssid and ssid_now == cfg.wifi_sms_ssid):
            await run(
                f'netsh wlan connect name="{cfg.wifi_dsl_ssid}" '
                f'ssid="{cfg.wifi_dsl_ssid}" interface="Wi-Fi"', timeout=30)
        await asyncio.sleep(1.5)
        await _set_preference(cfg, "eth")
        ok, src, ip = await _route_matches(cfg, "eth")
        if not ok:
            await asyncio.sleep(1.0)
            ok, src, ip = await _route_matches(cfg, "eth")
        if ok and remember:
            net_pref.set_source("eth")
        return {
            "ok": ok,
            "output": out if ok else f"route still on {src} ({ip})",
            "code": code,
            "source": "eth",
            "active": src,
            "ip": ip,
        }
    return {"ok": False, "error": "source must be eth|wifi|sms"}
