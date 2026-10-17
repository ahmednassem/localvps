"""hwtemp.py — best-effort CPU/GPU temperature on Windows.

Primary source: LibreHardwareMonitorLib.dll loaded via pythonnet. Drop the DLL
next to this file (or set LHM_DLL in .env). Fallback: WMI MSAcpi_ThermalZone
(often missing on laptops). Returns the max observed core/package temp in C, or
None if nothing is readable.

Loading the DLL requires the process to be elevated — which the service is
(LocalSystem) — so sensors are available.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("hwtemp")

_HERE = os.path.dirname(os.path.abspath(__file__))
_computer = None          # cached LibreHardwareMonitor Computer instance
_lhm_failed = False


def _try_init_lhm() -> bool:
    global _computer, _lhm_failed
    if _computer is not None:
        return True
    if _lhm_failed:
        return False
    dll = os.environ.get("LHM_DLL", "").strip() or \
        os.path.join(_HERE, "LibreHardwareMonitorLib.dll")
    if not os.path.exists(dll):
        _lhm_failed = True
        return False
    try:
        import clr  # type: ignore  (provided by pythonnet)
        clr.AddReference(dll)
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore

        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.Open()
        _computer = c
        log.info("LibreHardwareMonitor initialised")
        return True
    except Exception as exc:                        # noqa: BLE001
        log.warning("LibreHardwareMonitor unavailable: %s", exc)
        _lhm_failed = True
        return False


def _read_lhm() -> float | None:
    if not _try_init_lhm():
        return None
    best: float | None = None
    try:
        for hw in _computer.Hardware:
            hw.Update()
            for sensor in hw.Sensors:
                # SensorType.Temperature == 2
                if int(sensor.SensorType) == 2 and sensor.Value is not None:
                    val = float(sensor.Value)
                    if best is None or val > best:
                        best = val
    except Exception as exc:                        # noqa: BLE001
        log.debug("lhm read error: %s", exc)
    return best


def _read_wmi() -> float | None:
    try:
        import wmi  # type: ignore
        w = wmi.WMI(namespace="root\\wmi")
        temps = []
        for z in w.MSAcpi_ThermalZoneTemperature():
            # tenths of Kelvin -> Celsius
            temps.append((z.CurrentTemperature / 10.0) - 273.15)
        return max(temps) if temps else None
    except Exception as exc:                        # noqa: BLE001
        log.debug("wmi temp read error: %s", exc)
        return None


def read_temp_c() -> float | None:
    """Return the hottest CPU/GPU sensor in Celsius, or None if unknown."""
    val = _read_lhm()
    if val is not None:
        return round(val, 1)
    val = _read_wmi()
    return round(val, 1) if val is not None else None
