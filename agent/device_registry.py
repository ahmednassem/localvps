"""device_registry.py — physical USB port roles + network labels.

F2 assigns a role to a **USB port location** once (e.g. Port_#0002.Hub_#0002 → fan).
After that, status is "is anything Present on that port?" (= drawing power / connected),
not a specific VID/PID.

Roles: fan | sms | eth | ignore
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from config import HERE

log = logging.getLogger("device_registry")

REGISTRY_PATH = os.path.join(HERE, "devices.json")
ROLES = ("fan", "sms", "eth", "ignore")


@dataclass
class PnpDevice:
    instance_id: str
    friendly_name: str
    status: str
    present: bool = True
    dev_class: str = ""
    port: str = ""          # e.g. "USB Port 1 (Hub 2)"
    location: str = ""      # raw Port_#0001.Hub_#0002
    hub_instance: str = ""
    port_number: int | None = None

    @property
    def key(self) -> str:
        # Port location is the stable identity for F2 assignments
        return self.location or self.instance_id

    @property
    def label(self) -> str:
        if self.port:
            return f"{self.port} — {self.friendly_name}"
        return self.friendly_name


@dataclass
class Registry:
    # keyed by USB location string Port_#N.Hub_#M
    assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    wifi_dsl_ssid: str = ""
    wifi_sms_ssid: str = ""
    eth_adapter: str = ""

    def to_dict(self) -> dict:
        return {
            "assignments": self.assignments,
            "wifi_dsl_ssid": self.wifi_dsl_ssid,
            "wifi_sms_ssid": self.wifi_sms_ssid,
            "eth_adapter": self.eth_adapter,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        raw = dict(data.get("assignments") or {})
        # Migrate old instance-id keys → location keys
        fixed: dict[str, dict[str, Any]] = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            loc = str(entry.get("location") or "")
            if not loc and key.startswith("Port_#"):
                loc = key
            if not loc:
                # drop unmigratable junk
                continue
            entry = dict(entry)
            entry["location"] = loc
            entry.setdefault("port", _parse_port_label(loc))
            fixed[loc] = entry
        return cls(
            assignments=fixed,
            wifi_dsl_ssid=str(data.get("wifi_dsl_ssid") or ""),
            wifi_sms_ssid=str(data.get("wifi_sms_ssid") or ""),
            eth_adapter=str(data.get("eth_adapter") or ""),
        )


def load() -> Registry:
    if not os.path.isfile(REGISTRY_PATH):
        return Registry()
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            return Registry.from_dict(json.load(fh))
    except Exception as exc:                        # noqa: BLE001
        log.warning("devices.json read failed: %s", exc)
        return Registry()


def save(reg: Registry) -> None:
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg.to_dict(), fh, indent=2)
    os.replace(tmp, REGISTRY_PATH)
    log.info("devices.json saved")


def assign(reg: Registry, dev: PnpDevice, role: str) -> None:
    """Bind a physical USB port (location) to a role. One role → one port."""
    role = role.lower()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not dev.location:
        raise ValueError("device has no USB port location")

    if role in ("fan", "sms"):
        for loc, entry in list(reg.assignments.items()):
            if entry.get("role") == role and loc != dev.location:
                del reg.assignments[loc]

    hub_inst = (dev.hub_instance or "").strip()
    port_no = dev.port_number
    if not hub_inst or port_no is None:
        try:
            import usb_hub
            parsed = usb_hub.parse_location(dev.location)
            if parsed:
                if port_no is None:
                    port_no = parsed[0]
                if not hub_inst:
                    hub_inst = usb_hub.hub_map_from_pnp().get(parsed[1], "")
        except Exception:                           # noqa: BLE001
            pass

    entry: dict[str, Any] = {
        "role": role,
        "location": dev.location,
        "port": dev.port or _parse_port_label(dev.location),
        "friendly_name": dev.friendly_name,
        "detect": "hub_or_pnp",
    }
    if hub_inst:
        entry["hub_instance"] = hub_inst
    if port_no is not None:
        entry["port_number"] = int(port_no)
    reg.assignments[dev.location] = entry
    save(reg)


def location_meta(reg: Registry, role: str) -> dict[str, Any]:
    role = role.lower()
    for loc, entry in reg.assignments.items():
        if entry.get("role") == role:
            return dict(entry, location=entry.get("location") or loc)
    return {}


def unassign(reg: Registry, location: str) -> None:
    reg.assignments.pop(location, None)
    save(reg)


def role_for(reg: Registry, location_or_id: str) -> str | None:
    entry = reg.assignments.get(location_or_id)
    if entry:
        return entry.get("role")
    # also match by scanning location field
    for loc, entry in reg.assignments.items():
        if loc == location_or_id or entry.get("location") == location_or_id:
            return entry.get("role")
    return None


def location_for_role(reg: Registry, role: str) -> str:
    role = role.lower()
    for loc, entry in reg.assignments.items():
        if entry.get("role") == role:
            return str(entry.get("location") or loc)
    return ""


def pattern_for_role(reg: Registry, role: str) -> str:
    """Config value for fan/sms: port:<location>."""
    loc = location_for_role(reg, role)
    return f"port:{loc}" if loc else ""


def is_cable_eth(name: str, description: str = "") -> bool:
    """True for real NIC cable eth — not Wi-Fi / SMS Remote NDIS."""
    blob = f"{name} {description}".lower()
    if re.search(r"wi-?fi|wireless|wlan|bluetooth|ndis|remote ndis|virtual|vpn|loopback", blob):
        return False
    if re.search(r"realtek|intel|broadcom|killer|gbe|2\.5g|ethernet", blob):
        return True
    # bare adapter name "Ethernet" (not Ethernet 2 which is often NDIS)
    if name.strip().lower() == "ethernet":
        return True
    return False


def apply_to_config(cfg) -> None:
    """Port roles always win for fan/sms; eth prefers devices.json cable NIC."""
    reg = load()
    fan = pattern_for_role(reg, "fan")
    sms = pattern_for_role(reg, "sms")
    if fan:
        cfg.fan_device = fan
    if sms:
        cfg.sms_device = sms

    eth = (reg.eth_adapter or cfg.eth_adapter or "").strip()
    if eth and is_cable_eth(eth):
        cfg.eth_adapter = eth
    elif eth and not is_cable_eth(eth):
        log.warning("ignoring non-cable eth_adapter=%r (likely SMS NDIS)", eth)
        if not cfg.eth_adapter or not is_cable_eth(cfg.eth_adapter):
            cfg.eth_adapter = "Ethernet"  # HP Realtek cable NIC name

    if not cfg.wifi_dsl_ssid and reg.wifi_dsl_ssid:
        cfg.wifi_dsl_ssid = reg.wifi_dsl_ssid
    if not cfg.wifi_sms_ssid and reg.wifi_sms_ssid:
        cfg.wifi_sms_ssid = reg.wifi_sms_ssid


def _parse_port_label(location: str) -> str:
    """Turn 'Port_#0001.Hub_#0002' into 'USB Port 1 (Hub 2)'."""
    if not location:
        return ""
    m = re.search(r"Port_#0*(\d+)\.Hub_#0*(\d+)", location, re.I)
    if m:
        return f"USB Port {int(m.group(1))} (Hub {int(m.group(2))})"
    return location.strip()


def _parse_pnp_json(raw: str) -> list[PnpDevice]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception:                               # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[PnpDevice] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("InstanceId") or row.get("instance_id") or "")
        name = str(row.get("FriendlyName") or row.get("friendly_name") or "?")
        location = str(row.get("Location") or row.get("location") or "")
        if not location:
            continue
        present_raw = row.get("Present", True)
        present = bool(present_raw) if not isinstance(present_raw, str) \
            else present_raw.strip().lower() in ("1", "true", "yes")
        out.append(PnpDevice(
            instance_id=iid or location,
            friendly_name=name,
            status=str(row.get("Status") or row.get("status") or "?"),
            present=present,
            dev_class=str(row.get("Class") or row.get("class") or ""),
            location=location,
            port=_parse_port_label(location),
        ))
    return out


async def scan_pnp_devices() -> list[PnpDevice]:
    """List physical USB ports that currently have something Present.

    Built-in camera / fingerprint / bluetooth are skipped.
    """
    from winutil import powershell
    ps = r"""
$skipClass = @('HIDClass','Keyboard','Mouse','SoftwareDevice','Camera','Biometric','Bluetooth','Media','Image','Processor')
Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object {
    $_.Present -eq $true -and
    $_.InstanceId -like 'USB\*' -and
    $_.FriendlyName -and
    $_.FriendlyName -notmatch 'Root Hub|Generic Hub|USB Hub' -and
    $_.Class -notin $skipClass
  } |
  ForEach-Object {
    $loc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LocationInfo' -ErrorAction SilentlyContinue).Data
    if (-not $loc -or $loc -notmatch 'Port_#') { return }
    $bus = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' -ErrorAction SilentlyContinue).Data
    if ($bus -match 'Camera|Fingerprint|Bluetooth') { return }
    [PSCustomObject]@{
      FriendlyName = $_.FriendlyName
      Status       = $_.Status
      Class        = $_.Class
      InstanceId   = $_.InstanceId
      Location     = [string]$loc
      Present      = $true
    }
  } |
  ConvertTo-Json -Compress
"""
    code, out = await powershell(ps, timeout=45)
    if code != 0:
        log.warning("USB port scan failed (code=%s)", code)
        return []
    return _parse_pnp_json(out)


async def scan_hub_ports() -> list[PnpDevice]:
    """All ports on mapped hubs (including empty) — for F2 fan port labeling."""
    import asyncio
    import usb_hub

    def _list() -> list[dict]:
        return usb_hub.list_external_ports()

    rows = await asyncio.to_thread(_list)
    out: list[PnpDevice] = []
    for r in rows:
        out.append(PnpDevice(
            instance_id=r["location"],
            friendly_name=str(r.get("name") or "?"),
            status=str(r.get("status") or "?"),
            present=bool(r.get("connected")),
            location=r["location"],
            port=r["port"],
            hub_instance=str(r.get("hub_instance") or ""),
            port_number=int(r.get("port_number") or 0) or None,
        ))
    return out


async def scan_net_adapters() -> list[dict]:
    """Cable ethernet adapters only (excludes Wi-Fi + SMS Remote NDIS)."""
    from winutil import powershell
    ps = (
        "Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Select-Object Name, InterfaceDescription, Status, MacAddress | "
        "ConvertTo-Json -Compress"
    )
    code, out = await powershell(ps, timeout=20)
    if code != 0 or not out.strip():
        return []
    try:
        import json as _json
        data = _json.loads(out.strip())
        rows = data if isinstance(data, list) else [data]
    except Exception:                               # noqa: BLE001
        return []
    return [
        a for a in rows
        if is_cable_eth(str(a.get("Name") or ""), str(a.get("InterfaceDescription") or ""))
    ]


async def scan_wifi_profiles() -> list[str]:
    from winutil import run
    code, out = await run("netsh wlan show profiles", timeout=20)
    if code != 0:
        return []
    profiles: list[str] = []
    for line in out.splitlines():
        m = re.match(r"^\s*All User Profile\s*:\s*(.+)$", line, re.I)
        if m:
            profiles.append(m.group(1).strip())
    return profiles


def diff_devices(prev: dict[str, PnpDevice], current: list[PnpDevice]) -> tuple[list[PnpDevice], list[str]]:
    """Diff by USB port location (not instance id)."""
    cur_map = {d.location: d for d in current if d.location}
    added = [d for loc, d in cur_map.items() if loc not in prev]
    removed = [loc for loc in prev if loc not in cur_map]
    return added, removed
