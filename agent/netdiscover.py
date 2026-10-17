"""netdiscover.py — auto-detect ethernet adapter + Wi-Fi profiles on Windows.

If ETH_ADAPTER / WIFI_DSL_SSID / WIFI_SMS_SSID are blank in .env, we discover
them at core start and log the result. Explicit .env values always win.

Classification heuristics for two Wi-Fi profiles:
  - SMS / phone hotspot: names with mobile/hotspot/galaxy/iphone/etc.
  - DSL router Wi-Fi: the other saved profile (or the first non-hotspot profile)
"""
from __future__ import annotations

import json
import logging
import os
import re

from config import Config, HERE
from winutil import powershell, run

log = logging.getLogger("netdiscover")

_CACHE = os.path.join(HERE, "network.auto.json")

# Phone / hotspot profile name hints (case-insensitive)
_SMS_HINTS = re.compile(
    r"mobile|hotspot|tether|galaxy|iphone|android|huawei|honor|vivo|"
    r"pixel|oneplus|redmi|xiaomi|sms|lte|4g|5g|usb|rndis|ncm",
    re.I,
)


async def discover_eth_adapter() -> str:
    """First up physical Ethernet adapter (not Wi-Fi / virtual / VPN)."""
    ps = (
        "Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'Up' -and "
        "$_.Name -notmatch 'Wi-Fi|Wireless|WLAN|Loopback|Virtual|VPN|TAP|TUN|Hyper-V' "
        "-and ($_.InterfaceDescription -match 'Ethernet|GbE|2\\.5G|10G|Realtek|Intel|Broadcom|Killer' "
        "-or $_.MediaType -eq '802.3') } | "
        "Select-Object -First 1 -ExpandProperty Name"
    )
    code, out = await powershell(ps, timeout=20)
    name = (out or "").strip()
    if name:
        return name

    # fallback: any Up adapter that is not wireless
    ps2 = (
        "Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'Up' -and "
        "$_.Name -notmatch 'Wi-Fi|Wireless|WLAN|Loopback|Virtual|VPN' } | "
        "Select-Object -First 1 -ExpandProperty Name"
    )
    code, out = await powershell(ps2, timeout=20)
    return (out or "").strip()


async def discover_wifi_profiles() -> list[str]:
    """Saved Wi-Fi profile names from netsh."""
    code, out = await run("netsh wlan show profiles", timeout=20)
    if code != 0 or not out:
        return []
    profiles: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^All User Profile\s*:\s*(.+)$", line, re.I)
        if m:
            profiles.append(m.group(1).strip())
    return profiles


def _classify_wifi(profiles: list[str], sms_hint: str = "") -> tuple[str, str]:
    """Return (dsl_ssid, sms_ssid) from profile list."""
    if not profiles:
        return "", ""

    sms = ""
    if sms_hint:
        hint = sms_hint.lower()
        for p in profiles:
            if hint in p.lower():
                sms = p
                break

    if not sms:
        for p in profiles:
            if _SMS_HINTS.search(p):
                sms = p
                break

    rest = [p for p in profiles if p != sms]
    dsl = rest[0] if rest else ""
    # one profile only: treat as DSL; SMS slot stays empty until a 2nd profile exists
    if len(profiles) == 1 and not dsl:
        dsl = profiles[0]
    return dsl, sms


def _load_cache() -> dict:
    if not os.path.isfile(_CACHE):
        return {}
    try:
        with open(_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                               # noqa: BLE001
        return {}


def _save_cache(data: dict) -> None:
    try:
        with open(_CACHE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:                        # noqa: BLE001
        log.warning("could not write %s: %s", _CACHE, exc)


async def resolve(cfg: Config) -> None:
    """Fill blank network fields on cfg (mutates in place)."""
    import device_registry as dr
    dr.apply_to_config(cfg)

    if not cfg.net_auto_discover:
        return

    cached = _load_cache()
    changed = False

    # fill blank eth from discovery only if it's a cable NIC
    if not cfg.eth_adapter:
        discovered = await discover_eth_adapter()
        if discovered and "ndis" not in discovered.lower():
            cfg.eth_adapter = discovered or cached.get("eth_adapter", "")
            changed = bool(cfg.eth_adapter)

    profiles = await discover_wifi_profiles()
    if profiles:
        log.info("discovered Wi-Fi profiles: %s", ", ".join(profiles))

    dsl, sms = _classify_wifi(profiles, sms_hint=cfg.sms_device)
    if not cfg.wifi_dsl_ssid:
        cfg.wifi_dsl_ssid = dsl or cached.get("wifi_dsl_ssid", "")
        if cfg.wifi_dsl_ssid:
            changed = True
    if not cfg.wifi_sms_ssid:
        cfg.wifi_sms_ssid = sms or cached.get("wifi_sms_ssid", "")
        if cfg.wifi_sms_ssid:
            changed = True

    if changed or profiles:
        _save_cache({
            "eth_adapter": cfg.eth_adapter,
            "wifi_dsl_ssid": cfg.wifi_dsl_ssid,
            "wifi_sms_ssid": cfg.wifi_sms_ssid,
            "all_wifi_profiles": profiles,
        })

    log.info(
        "network config: eth=%r dsl_wifi=%r sms_wifi=%r (auto=%s)",
        cfg.eth_adapter or "(none)",
        cfg.wifi_dsl_ssid or "(none)",
        cfg.wifi_sms_ssid or "(none)",
        cfg.net_auto_discover,
    )
