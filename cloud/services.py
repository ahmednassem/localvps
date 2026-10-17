"""services.py — per-device service bundle shared by gate, bot and scheduler."""
from __future__ import annotations

from dataclasses import dataclass

from alerts import Watchdog
from netrotate import NetRotate
from night import NightWatch
from power import SmartPlug
from vps_power import VpsPower


@dataclass
class DeviceServices:
    plug: SmartPlug
    vps: VpsPower
    night: NightWatch
    netrotate: NetRotate
    watchdog: Watchdog
