"""hub.py — in-memory state shared by the gate, bot, watchdogs and output API.

Multi-device: the hub holds one DeviceState per device (device 1 or
device 2). Each device has its own control tunnel (/agent/ws), HTTP
heartbeat, intentional-offline flag and dead-man suppression window. Commands
are addressed to a device id.

Also keeps a ring buffer of recent events/alerts for the output API and fires
`on_device_change` so the lsvc feed can tell the local services which device
is working.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable, Optional

import vps_state


class ControlLink:
    """One live control-tunnel connection from a device core."""

    def __init__(self, send) -> None:
        self._send = send                       # async callable: dict -> None
        self.host: str = "?"
        self.device_id: int = 0
        self.connected_at: float = time.time()
        self.last_seen: float = time.time()
        self.last_telemetry: dict[str, Any] = {}

    async def send(self, frame: dict) -> None:
        await self._send(frame)


class DeviceState:
    """Everything the gate knows about one device."""

    def __init__(self, device_id: int, label: str) -> None:
        self.id = device_id
        self.label = label
        self.control: Optional[ControlLink] = None
        # HTTP status push (POST /status every ~60s)
        self.http_last_seen: float = 0.0
        self.http_telemetry: dict[str, Any] = {}
        self.http_host: str = "?"
        # True after /offline — suppress dead-man until /online
        self.intentional_offline: bool = vps_state.load(self.state_path)
        # while time.time() < this, the dead-man must not plug-cycle
        self.suppress_deadman_until: float = 0.0
        # set by health.Health every tick (None until the first tick)
        self.health_case: Optional[str] = None
        self.health_text: str = ""
        self.health_since: float = 0.0
        self.power_hold: bool = False
        self.deadman_ok: Optional[bool] = None
        # set by cluster.Cluster every tick
        self.node_state: Optional[str] = None
        self.node_since: float = 0.0
        self.peer_seen: bool = False

    @property
    def running(self) -> bool:
        return self.control is not None or self.peer_seen

    @property
    def state_path(self) -> str:
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(here, f"vps_state_{self.id}.json")

    def persist_offline_flag(self) -> None:
        vps_state.save(self.intentional_offline, self.state_path)

    # ── health ────────────────────────────────────────────────────────────

    def commands_available(self) -> bool:
        return self.control is not None

    def online(self) -> bool:
        return self.seconds_since_status() <= 150.0

    def seconds_since_status(self) -> float:
        if self.http_last_seen <= 0:
            if self.control is not None:
                return time.time() - self.control.last_seen
            return float("inf")
        return time.time() - self.http_last_seen

    def note_http_status(self, payload: dict) -> None:
        self.http_last_seen = time.time()
        self.http_telemetry = payload
        self.http_host = str(payload.get("host", self.http_host))

    def telemetry(self) -> dict:
        if self.http_telemetry:
            return self.http_telemetry
        if self.control is not None:
            return self.control.last_telemetry or {}
        return {}

    def status_snapshot(self) -> dict:
        c = self.control
        tele = self.telemetry()
        secs = self.seconds_since_status()
        if secs == float("inf"):
            secs = None
        last_at = self.http_last_seen or None
        devices = tele.get("devices") if isinstance(tele.get("devices"), dict) else {}
        plugged = tele.get("plugged")
        if plugged is None:
            plugged = tele.get("ac_connected")
        return {
            "device": self.id,
            "label": self.label,
            "online": self.online(),
            "intentional_offline": self.intentional_offline,
            "host": self.http_host if self.http_last_seen else (c.host if c else None),
            "last_status_at": last_at,
            "seconds_since_status": secs,
            "command_channel": "connected" if c else "down",
            "telemetry": tele,
            "net": tele.get("net"),
            "ip": tele.get("ip"),
            "internet": tele.get("internet"),
            "battery_pct": tele.get("battery_pct", tele.get("battery")),
            "plugged": plugged,
            "ac_connected": tele.get("ac_connected"),
            "charging": tele.get("charging"),
            "power": tele.get("power"),
            "devices": devices,
            "home": tele.get("home") or {},
            "peer": tele.get("peer") or {},
            "health": {"case": self.health_case, "text": self.health_text,
                       "since": self.health_since, "hold": self.power_hold},
            "node": {"state": self.node_state, "since": self.node_since,
                     "running": self.running, "peer_seen": self.peer_seen},
        }


# on_event(device_id, message, level); on_device_change(device_id)
EventCb = Callable[[int, str, str], Awaitable[None]]
ChangeCb = Callable[[int], Awaitable[None]]


class Hub:
    def __init__(self, device_labels: dict[int, str] | None = None) -> None:
        labels = device_labels or {1: "laptop1", 2: "laptop2"}
        self.devices: dict[int, DeviceState] = {
            did: DeviceState(did, label) for did, label in labels.items()}
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.on_event: Optional[EventCb] = None
        self.on_device_change: Optional[ChangeCb] = None
        # ring buffer for the output API: {"ts","device","level","message"}
        self.events: deque = deque(maxlen=500)

    # ── device access ─────────────────────────────────────────────────────

    def device(self, device_id: int) -> DeviceState:
        dev = self.devices.get(int(device_id))
        if dev is None:
            raise KeyError(f"unknown device {device_id}")
        return dev

    def has_device(self, device_id) -> bool:
        try:
            return int(device_id) in self.devices
        except (TypeError, ValueError):
            return False

    def add_event(self, device_id: int, message: str, level: str = "info") -> None:
        self.events.append({
            "ts": time.time(),
            "device": int(device_id) if device_id else None,
            "level": level,
            "message": message,
        })

    async def _fire_change(self, device_id: int) -> None:
        if self.on_device_change is None:
            return
        try:
            await self.on_device_change(device_id)
        except Exception:                            # noqa: BLE001
            pass

    # ── control link lifecycle ────────────────────────────────────────────

    async def attach_control(self, device_id: int, link: ControlLink) -> None:
        dev = self.device(device_id)
        link.device_id = dev.id
        async with self._lock:
            # A new connection does NOT fail pending commands. Switching a
            # device's default route (net rotation, router reboot interlock)
            # tears down the old TCP connection mid-command; the agent keeps
            # running the command and sends the result — same id — over the
            # new link. Each waiter is already bounded by its own timeout.
            dev.control = link
        await self._fire_change(dev.id)

    async def detach_control(self, device_id: int, link: ControlLink) -> None:
        dev = self.device(device_id)
        async with self._lock:
            if dev.control is link:
                dev.control = None
                # keep pending: a reconnect usually follows within seconds and
                # delivers the result; otherwise wait_for() times out.
        await self._fire_change(dev.id)

    # ── telemetry / heartbeat ─────────────────────────────────────────────

    def note_telemetry(self, device_id: int, payload: dict) -> None:
        """Legacy: telemetry over WebSocket (optional duplicate)."""
        dev = self.devices.get(int(device_id))
        if dev is not None and dev.control is not None:
            dev.control.last_seen = time.time()
            dev.control.last_telemetry = payload

    def note_http_status(self, device_id: int, payload: dict) -> None:
        """Primary heartbeat: device POSTs /status every ~60s."""
        dev = self.device(device_id)
        was_online = dev.online()
        dev.note_http_status(payload)
        if not was_online:
            asyncio.get_running_loop().create_task(self._fire_change(dev.id))

    def status_snapshot(self, device_id: int) -> dict:
        return self.device(device_id).status_snapshot()

    def all_snapshots(self) -> list[dict]:
        return [self.devices[d].status_snapshot()
                for d in sorted(self.devices)]

    # ── command dispatch (request/response over a control tunnel) ─────────

    async def send_command(self, device_id: int, kind: str, name: str,
                           args: Optional[dict] = None,
                           timeout: float = 30.0) -> dict:
        """Push a command to one device and wait for its matching result.

        Returns the result frame ({"ok":bool,"output":...,"code":int,...}).
        Raises RuntimeError if the device is offline and TimeoutError on
        timeout.
        """
        dev = self.device(device_id)
        link = dev.control
        if link is None:
            raise RuntimeError(
                f"{dev.label}: command channel offline (WebSocket down)")

        cmd_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = fut

        frame = {"type": "cmd", "id": cmd_id, "kind": kind,
                 "name": name, "args": args or {}}
        try:
            await link.send(frame)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(cmd_id, None)

    def resolve_result(self, frame: dict) -> None:
        cmd_id = frame.get("id")
        fut = self._pending.get(cmd_id)
        if fut is not None and not fut.done():
            fut.set_result(frame)
