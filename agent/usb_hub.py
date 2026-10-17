"""usb_hub.py — query USB hub port connection (even for power-only plugs).

A dumb USB fan often has no PnP device — Windows never enumerates it.
The root/hub driver still reports ConnectionStatus=DeviceConnected on that
port when something loads the data lines, and we can map LocationInfo
Port_#N.Hub_#M via sibling devices' parent hub.

For pure VBUS-only loads with no D+/D- pull-up, the hub may still report
NoDeviceConnected — then software cannot see the plug.
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger("usb_hub")

# GUID_DEVINTERFACE_USB_HUB
GUID_DEVINTERFACE_USB_HUB = "{f18a0e88-c30c-11d0-8815-00a0c906bed8}"

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = 0x00220448
IOCTL_USB_GET_NODE_INFORMATION = 0x00220408

USB_CONNECTION_STATUS = {
    0: "NoDeviceConnected",
    1: "DeviceConnected",
    2: "DeviceFailedEnumeration",
    3: "DeviceGeneralFailure",
    4: "DeviceCausedOvercurrent",
    5: "DeviceNotEnoughPower",
    6: "DeviceNotEnoughBandwidth",
    7: "DeviceHubNestedTooDeeply",
    8: "DeviceInLegacyHub",
    9: "DeviceEnumerating",
    10: "DeviceReset",
}


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


class USB_DEVICE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bcdUSB", ctypes.c_ushort),
        ("bDeviceClass", ctypes.c_ubyte),
        ("bDeviceSubClass", ctypes.c_ubyte),
        ("bDeviceProtocol", ctypes.c_ubyte),
        ("bMaxPacketSize0", ctypes.c_ubyte),
        ("idVendor", ctypes.c_ushort),
        ("idProduct", ctypes.c_ushort),
        ("bcdDevice", ctypes.c_ushort),
        ("iManufacturer", ctypes.c_ubyte),
        ("iProduct", ctypes.c_ubyte),
        ("iSerialNumber", ctypes.c_ubyte),
        ("bNumConfigurations", ctypes.c_ubyte),
    ]


class USB_NODE_CONNECTION_INFORMATION_EX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ConnectionIndex", wintypes.ULONG),
        ("DeviceDescriptor", USB_DEVICE_DESCRIPTOR),
        ("CurrentConfigurationValue", ctypes.c_ubyte),
        ("Speed", ctypes.c_ubyte),
        ("DeviceIsHub", ctypes.c_ubyte),
        ("DeviceAddress", ctypes.c_ushort),
        ("NumberOfOpenPipes", wintypes.ULONG),
        ("ConnectionStatus", wintypes.ULONG),
    ]


class USB_HUB_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bDescriptorLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bNumberOfPorts", ctypes.c_ubyte),
        ("wHubCharacteristics", ctypes.c_ushort),
        ("bPowerOnToPowerGood", ctypes.c_ubyte),
        ("bHubControlCurrent", ctypes.c_ubyte),
        ("bRemoveAndPowerMask", ctypes.c_ubyte * 64),
    ]


class USB_HUB_INFORMATION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("HubDescriptor", USB_HUB_DESCRIPTOR),
        ("HubIsBusPowered", ctypes.c_ubyte),
    ]


class USB_NODE_INFORMATION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("NodeType", wintypes.ULONG),  # 0 = UsbHub
        ("HubInformation", USB_HUB_INFORMATION),
    ]


setupapi = ctypes.windll.setupapi
kernel32 = ctypes.windll.kernel32

setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
    wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def _guid_from_str(s: str) -> GUID:
    import uuid
    u = uuid.UUID(s)
    g = GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


@dataclass
class HubPort:
    hub_path: str
    hub_instance: str   # e.g. USB\ROOT_HUB30\5&1a2b3c4d&0&0
    port_number: int
    connected: bool
    connection_status: int
    location: str = ""  # Port_#N.Hub_#M when known


def _instance_from_path(path: str) -> str:
    # Example: \\?\usb#root_hub30#5&1a2b3c4d&0&0#{guid}
    #       -> USB\ROOT_HUB30\5&1a2b3c4d&0&0
    m = re.search(r"\\\\[?]\\usb#([^#]+)#([^#]+)#", path, re.I)
    if not m:
        return ""
    return f"USB\\{m.group(1)}\\{m.group(2)}"


def list_hub_device_paths() -> list[tuple[str, str]]:
    """Return list of (symbolic_link_path, instance_id_guess)."""
    guid = _guid_from_str(GUID_DEVINTERFACE_USB_HUB)
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == ctypes.c_void_p(-1).value:
        return []
    out: list[tuple[str, str]] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            needed = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(iface), None, 0, ctypes.byref(needed), None)
            buf = ctypes.create_string_buffer(needed.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA.cbSize = 8 on x64, 6 on x86
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(iface), buf, needed,
                    ctypes.byref(needed), None):
                # device path starts after cbSize
                offset = 4
                path = ctypes.wstring_at(ctypes.addressof(buf) + offset)
                if not path or path.startswith("\x00"):
                    path = ctypes.wstring_at(ctypes.addressof(buf) + 8)
                inst = _instance_from_path(path)
                out.append((path, inst))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def _open_hub(path: str):
    handle = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    return handle


def hub_port_count(path: str) -> int:
    h = _open_hub(path)
    if not h:
        return 0
    try:
        info = USB_NODE_INFORMATION()
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            h, IOCTL_USB_GET_NODE_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(ret), None,
        )
        if not ok:
            return 8
        return int(info.HubInformation.HubDescriptor.bNumberOfPorts) or 8
    finally:
        kernel32.CloseHandle(h)


def hub_port_connected(path: str, port: int) -> tuple[bool, int]:
    """Return (connected, connection_status) for hub path + 1-based port."""
    h = _open_hub(path)
    if not h:
        return False, -1
    try:
        info = USB_NODE_CONNECTION_INFORMATION_EX()
        info.ConnectionIndex = port
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
            ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(ret), None,
        )
        if not ok:
            return False, -1
        status = int(info.ConnectionStatus)
        # DeviceConnected=1, FailedEnumeration=2 still means something plugged
        connected = status in (1, 2, 3, 4, 5, 9, 10) or int(info.DeviceAddress) != 0
        return connected, status
    finally:
        kernel32.CloseHandle(h)


_LOC_RE = re.compile(r"Port_#0*(\d+)\.Hub_#0*(\d+)", re.I)


def parse_location(location: str) -> tuple[int, int] | None:
    m = _LOC_RE.search(location or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def hub_map_from_pnp() -> dict[int, str]:
    """Map LocationInfo Hub_#N → hub instance id via present USB children."""
    import subprocess
    ps = r"""
Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object { $_.Present -eq $true -and $_.InstanceId -like 'USB\VID*' } |
  ForEach-Object {
    $loc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LocationInfo' -EA SilentlyContinue).Data
    $par = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Parent' -EA SilentlyContinue).Data
    if ($loc -match 'Hub_#0*(\d+)' -and $par) {
      "$(${matches}[1])|$par"
    }
  }
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True, timeout=40,
            creationflags=0x08000000,
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("hub map scan failed: %s", exc)
        return {}
    mapping: dict[int, str] = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        hub_n, _, parent = line.partition("|")
        try:
            mapping[int(hub_n)] = parent.strip()
        except ValueError:
            continue
    return mapping


def resolve_hub_path(hub_instance: str, paths: list[tuple[str, str]] | None = None) -> str | None:
    paths = paths or list_hub_device_paths()
    want = hub_instance.upper().replace("/", "\\")
    want_tail = want.split("\\")[-1]
    for path, inst in paths:
        iu = inst.upper().replace("/", "\\")
        if iu == want or iu.replace("ROOT_HUB30", "ROOT_HUB30") == want:
            return path
        if want_tail and want_tail.upper() in path.upper():
            return path
        # case-insensitive full USB\ROOT_HUB30\xxx match ignoring ROOT case
        if iu.split("\\")[-1].upper() == want_tail.upper():
            return path
    return None


def port_connected_by_location(
    location: str,
    hub_instance: str | None = None,
    port_number: int | None = None,
) -> bool | None:
    """True/False if hub reports a connection; None if we cannot query."""
    parsed = parse_location(location)
    if port_number is None:
        if not parsed:
            return None
        port_number = parsed[0]
    if not hub_instance:
        if not parsed:
            return None
        hub_n = parsed[1]
        mapping = hub_map_from_pnp()
        hub_instance = mapping.get(hub_n)
        if not hub_instance:
            return None
    path = resolve_hub_path(hub_instance)
    if not path:
        # try all hubs — match port that is uniquely connected? no, use hub_instance only
        return None
    connected, status = hub_port_connected(path, port_number)
    log.debug("hub %s port %s → connected=%s status=%s",
              hub_instance, port_number, connected,
              USB_CONNECTION_STATUS.get(status, status))
    return connected


def list_external_ports() -> list[dict]:
    """Ports on hubs that already have at least one labeled LocationInfo sibling.

    Returns dicts: location, port, hub_instance, port_number, connected, name.
    """
    mapping = hub_map_from_pnp()  # Hub_# → instance
    paths = list_hub_device_paths()
    # reverse: instance → Hub_# (prefer lower Hub_# already mapped)
    inst_to_hubn = {v.upper(): k for k, v in mapping.items()}
    rows: list[dict] = []
    for hub_n, inst in mapping.items():
        path = resolve_hub_path(inst, paths)
        if not path:
            continue
        nports = hub_port_count(path) or 8
        for p in range(1, nports + 1):
            connected, status = hub_port_connected(path, p)
            loc = f"Port_#{p:04d}.Hub_#{hub_n:04d}"
            rows.append({
                "location": loc,
                "port": f"USB Port {p} (Hub {hub_n})",
                "hub_instance": inst,
                "port_number": p,
                "connected": connected,
                "status": USB_CONNECTION_STATUS.get(status, str(status)),
                "name": "powered / connected" if connected else "(empty)",
            })
    return rows
