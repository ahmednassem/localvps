"""devices.py — USB port power / presence + enable/disable.

fan/sms are tracked by **physical USB port location** (from F2 / devices.json):
  port:Port_#0002.Hub_#0002

status:
  on     → something Present on that port (device taking power / connected)
  absent → nothing Present on that port
  off    → present but disabled in Windows
"""
from __future__ import annotations

import json
import logging
import re

from winutil import powershell

log = logging.getLogger("devices")

_PORT_RE = re.compile(r"^(?:port:)?(Port_#\d+\.Hub_#\d+)$", re.I)


def _parse_port(pattern: str) -> str | None:
    if not pattern:
        return None
    m = _PORT_RE.match(pattern.strip())
    return m.group(1) if m else None


def _match_expr(pattern: str) -> str:
    safe = pattern.replace("'", "''")
    return (
        f"($_.FriendlyName -like '*{safe}*' -or $_.InstanceId -like '*{safe}*')"
    )


async def port_power_status(location: str, hub_instance: str = "",
                            port_number: int | None = None) -> str:
    """Is this physical USB port powered / occupied?

    1) PnP Present device on that LocationInfo → on
    2) Else USB hub ConnectionStatus (sees many power-only plugs) → on/absent
    """
    if not location:
        return "absent"
    safe = location.replace("'", "''")
    ps = (
        f"$want = '{safe}'; $hit = $false; $disabled = $false; "
        "Get-PnpDevice -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Present -eq $true -and $_.InstanceId -like 'USB*' } | "
        "ForEach-Object { "
        "  $loc = [string](Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "    -KeyName 'DEVPKEY_Device_LocationInfo' -ErrorAction SilentlyContinue).Data; "
        "  if ($loc -eq $want) { "
        "    $hit = $true; "
        "    if ($_.Problem -eq 22) { $disabled = $true } "
        "  } "
        "}; "
        "if (-not $hit) { 'absent' } "
        "elseif ($disabled) { 'off' } "
        "else { 'on' }"
    )
    code, out = await powershell(ps, timeout=35)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    val = (lines[-1] if lines else "").lower()
    if val == "on":
        return "on"
    if val == "off":
        return "off"

    # Fan / charge-only: no PnP device — ask the USB hub if the port is occupied
    try:
        import asyncio
        import usb_hub

        def _probe() -> bool | None:
            return usb_hub.port_connected_by_location(
                location, hub_instance=hub_instance or None,
                port_number=port_number)

        connected = await asyncio.to_thread(_probe)
        if connected is True:
            return "on"
        if connected is False:
            return "absent"
    except Exception as exc:                        # noqa: BLE001
        log.warning("hub port probe failed for %s: %s", location, exc)

    if val == "absent":
        return "absent"
    log.warning("port_power_status(%s) unexpected: code=%s out=%r", location, code, out)
    return "unknown"


async def status(pattern: str) -> str:
    """pattern is either port:Port_#x.Hub_#y or a legacy name/VID substring."""
    if not pattern:
        return "absent"
    port = _parse_port(pattern)
    if port:
        return await port_power_status(port)

    ps = (
        "$d = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {"
        + _match_expr(pattern) +
        " -and $_.Present -eq $true} | "
        "Select-Object -First 1 FriendlyName,Status,Problem,InstanceId,Present; "
        "if ($null -eq $d) { 'absent' } "
        "elseif ($d.Problem -eq 22 -or $d.Status -eq 'Error') { 'off' } "
        "else { 'on' }"
    )
    code, out = await powershell(ps, timeout=25)
    out = (out or "").strip().lower()
    if out in ("on", "off", "absent"):
        return out
    return "unknown"


async def set_enabled(pattern: str, enabled: bool) -> dict:
    if not pattern:
        return {"ok": False, "error": "device pattern not configured"}
    verb = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
    port = _parse_port(pattern)
    if port:
        safe = port.replace("'", "''")
        ps = rf"""
$locWanted = '{safe}'
$n = 0
Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {{
  $_.InstanceId -like 'USB\*' -and $_.Present -eq $true
}} | ForEach-Object {{
  $loc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LocationInfo' -ErrorAction SilentlyContinue).Data
  if ([string]$loc -eq $locWanted) {{
    {verb} -InstanceId $_.InstanceId -Confirm:$false
    $n++
  }}
}}
if ($n -eq 0) {{ Write-Output 'absent'; exit 3 }}
Write-Output "done:$n"
"""
    else:
        ps = (
            "$d = Get-PnpDevice | Where-Object {" + _match_expr(pattern) + "}; "
            "if ($null -eq $d) { Write-Output 'absent'; exit 3 } "
            "$d | ForEach-Object { " + verb + " -InstanceId $_.InstanceId -Confirm:$false }; "
            "Write-Output 'done'"
        )
    code, out = await powershell(ps, timeout=40)
    ok = code == 0 and "done" in (out or "")
    return {"ok": ok, "output": out, "code": code}


async def details(pattern: str) -> dict:
    if not pattern:
        return {"ok": False, "error": "device pattern not configured"}
    port = _parse_port(pattern)
    if port:
        st = await port_power_status(port)
        return {"ok": True, "port": port, "power": st}
    ps = (
        "Get-PnpDevice | Where-Object {" + _match_expr(pattern) + "} | "
        "Select-Object FriendlyName,Status,Class,InstanceId,Present | ConvertTo-Json -Compress"
    )
    code, out = await powershell(ps, timeout=25)
    try:
        data = json.loads(out) if out else None
    except Exception:                               # noqa: BLE001
        data = out
    return {"ok": code == 0, "device": data, "code": code}
