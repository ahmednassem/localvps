"""wlan_scan.py — forced Wi‑Fi scan via Windows Native WLAN API (wlanapi.dll).

`netsh wlan show networks` often only returns the *current* SSID while
associated. WlanScan + WlanGetAvailableNetworkList is more complete.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("wlan_scan")

wlanapi = ctypes.WinDLL("wlanapi")

ERROR_SUCCESS = 0
WLAN_AVAILABLE_NETWORK_INCLUDE_ALL_ADHOC_PROFILES = 0x00000001
WLAN_AVAILABLE_NETWORK_INCLUDE_ALL_MANUAL_HIDDEN_PROFILES = 0x00000002


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", wintypes.DWORD),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [
        ("uSSIDLength", wintypes.ULONG),
        ("ucSSID", ctypes.c_ubyte * 32),
    ]


class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [
        ("strProfileName", ctypes.c_wchar * 256),
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", wintypes.DWORD),
        ("uNumberOfBssids", wintypes.ULONG),
        ("bNetworkConnectable", wintypes.BOOL),
        ("wlanNotConnectableReason", wintypes.DWORD),
        ("uNumberOfPhyTypes", wintypes.ULONG),
        ("dot11PhyTypes", wintypes.DWORD * 8),
        ("bMorePhyTypes", wintypes.BOOL),
        ("wlanSignalQuality", wintypes.ULONG),
        ("bSecurityEnabled", wintypes.BOOL),
        ("dot11DefaultAuthAlgorithm", wintypes.DWORD),
        ("dot11DefaultCipherAlgorithm", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwReserved", wintypes.DWORD),
    ]


class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("Network", WLAN_AVAILABLE_NETWORK * 1),
    ]


wlanapi.WlanOpenHandle.argtypes = [
    wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p),
]
wlanapi.WlanOpenHandle.restype = wintypes.DWORD

wlanapi.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
wlanapi.WlanCloseHandle.restype = wintypes.DWORD

wlanapi.WlanEnumInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)),
]
wlanapi.WlanEnumInterfaces.restype = wintypes.DWORD

wlanapi.WlanScan.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p,
]
wlanapi.WlanScan.restype = wintypes.DWORD

wlanapi.WlanGetAvailableNetworkList.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)),
]
wlanapi.WlanGetAvailableNetworkList.restype = wintypes.DWORD

wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
wlanapi.WlanFreeMemory.restype = None


def _ssid_from_dot11(ssid: DOT11_SSID) -> str:
    n = int(ssid.uSSIDLength)
    if n <= 0:
        return ""
    raw = bytes(ssid.ucSSID[:n])
    try:
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception:                                # noqa: BLE001
        return raw.decode("latin-1", errors="ignore").strip()


def _scan_ssids_sync(wait_s: float = 4.0) -> list[str]:
    """Force scan and return unique SSID strings (may include current)."""
    client = ctypes.c_void_p()
    negotiated = wintypes.DWORD()
    rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated),
                                ctypes.byref(client))
    if rc != ERROR_SUCCESS:
        log.debug("WlanOpenHandle failed: %s", rc)
        return []

    ifaces = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
    found: list[str] = []
    try:
        rc = wlanapi.WlanEnumInterfaces(client, None, ctypes.byref(ifaces))
        if rc != ERROR_SUCCESS or not ifaces:
            log.debug("WlanEnumInterfaces failed: %s", rc)
            return []

        n = int(ifaces.contents.dwNumberOfItems)
        # Flexible array: re-cast to real size
        class _LIST(ctypes.Structure):
            _fields_ = [
                ("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", WLAN_INTERFACE_INFO * max(n, 1)),
            ]

        lst = ctypes.cast(ifaces, ctypes.POINTER(_LIST)).contents
        for i in range(n):
            iface = lst.InterfaceInfo[i]
            guid = iface.InterfaceGuid
            # Kick a scan (non-blocking); then wait and read list
            wlanapi.WlanScan(client, ctypes.byref(guid), None, None, None)

        import time as _time
        _time.sleep(max(1.5, float(wait_s)))

        for i in range(n):
            iface = lst.InterfaceInfo[i]
            guid = iface.InterfaceGuid
            nets_ptr = ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
            flags = (
                WLAN_AVAILABLE_NETWORK_INCLUDE_ALL_ADHOC_PROFILES
                | WLAN_AVAILABLE_NETWORK_INCLUDE_ALL_MANUAL_HIDDEN_PROFILES
            )
            rc = wlanapi.WlanGetAvailableNetworkList(
                client, ctypes.byref(guid), flags, None, ctypes.byref(nets_ptr))
            if rc != ERROR_SUCCESS or not nets_ptr:
                continue
            try:
                count = int(nets_ptr.contents.dwNumberOfItems)

                class _NETS(ctypes.Structure):
                    _fields_ = [
                        ("dwNumberOfItems", wintypes.DWORD),
                        ("dwIndex", wintypes.DWORD),
                        ("Network", WLAN_AVAILABLE_NETWORK * max(count, 1)),
                    ]

                nets = ctypes.cast(nets_ptr, ctypes.POINTER(_NETS)).contents
                for j in range(count):
                    name = _ssid_from_dot11(nets.Network[j].dot11Ssid)
                    if name and name not in found:
                        found.append(name)
            finally:
                wlanapi.WlanFreeMemory(nets_ptr)
    finally:
        if ifaces:
            wlanapi.WlanFreeMemory(ifaces)
        wlanapi.WlanCloseHandle(client, None)

    return found


_cache_names: list[str] = []
_cache_at: float = 0.0
_cache_lock = asyncio.Lock()
_CACHE_TTL = 45.0  # avoid hammering WlanScan from telemetry + keepalives


async def list_ssids(wait_s: float = 4.0, *, force: bool = False) -> list[str]:
    import time

    global _cache_names, _cache_at
    now = time.time()
    if not force and _cache_names and (now - _cache_at) < _CACHE_TTL:
        return list(_cache_names)
    async with _cache_lock:
        now = time.time()
        if not force and _cache_names and (now - _cache_at) < _CACHE_TTL:
            return list(_cache_names)
        names = await asyncio.to_thread(_scan_ssids_sync, wait_s)
        if names:
            _cache_names = list(names)
            _cache_at = time.time()
        elif _cache_names and (now - _cache_at) < (_CACHE_TTL * 2):
            # keep last good briefy on empty/failed scan
            return list(_cache_names)
        return names


async def ssid_visible(want: str, wait_s: float = 4.0) -> tuple[bool | None, list[str]]:
    """Return (result, scanned_list).

    True  — SSID seen in forced scan
    False — scan returned several networks but SSID missing (likely off)
    None  — inconclusive (empty / only current network listed)
    """
    want = (want or "").strip()
    if not want:
        return False, []
    try:
        names = await list_ssids(wait_s)
    except Exception as exc:                         # noqa: BLE001
        log.warning("wlan scan failed: %s", exc)
        return None, []

    want_l = want.lower()
    if any(n.lower() == want_l for n in names):
        return True, names
    # One or zero networks → often "current only"; not trustworthy for "off"
    if len(names) <= 1:
        return None, names
    return False, names
