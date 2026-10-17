"""cluster.py — one node state per device (cluster plan phase 3).

Every POLL seconds the VPS turns what it knows about a device into one state:

  online         running: control link, a fresh beat, or the other device
                 sees it over the peer link
  booting        not running yet, inside the wake grace after a plug cycle
  shutting_down  switched off by us and still running, or the plug cut
                 after the shutdown is still pending
  off            not running and its plug is off or draws under the minimum
                 (or switched off by us and the cut is done)
  unreachable    not running, plug drawing power or unknown

The power rules read the result from the hub's DeviceState:
  node_state   the state above
  peer_seen    the other device has a fresh peer link to it
  running      control link or peer_seen (vps_power never cuts while True)

Transitions are logged and kept (last EVENTS_KEEP) for /api/cluster.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from hub import Hub

log = logging.getLogger("cluster")

STATES = ("online", "booting", "shutting_down", "off", "unreachable")
EVENTS_KEEP = 100


@dataclass
class NodeInputs:
    control: bool = False
    seen: bool = False
    peer_seen: bool = False
    intentional_off: bool = False
    cut_pending: bool = False
    waking: bool = False
    socket: bool | None = None
    watts: float | None = None
    min_watts: float = 5.0


def node_state(i: NodeInputs) -> str:
    running = i.control or i.seen or i.peer_seen
    if i.intentional_off:
        return "shutting_down" if running or i.cut_pending else "off"
    if running:
        return "online"
    if i.waking:
        return "booting"
    if i.socket is False:
        return "off"
    if i.socket and i.watts is not None and i.watts < i.min_watts:
        return "off"
    return "unreachable"


class Cluster:
    def __init__(self, hub: Hub, services: dict, health, *,
                 poll: float = 10.0, notify=None,
                 version_grace_s: float = 600.0) -> None:
        self._hub = hub
        self._services = services            # {device_id: DeviceServices}
        self._health = health
        self._poll = poll
        self._notify = notify
        self._version_grace_s = version_grace_s
        self._versions_differ_since: float | None = None
        self._versions_alerted = False
        self.events: deque[dict] = deque(maxlen=EVENTS_KEEP)

    def versions(self) -> dict[int, str]:
        out = {}
        for d in sorted(self._services):
            dev = self._hub.device(d)
            v = (dev.telemetry() or {}).get("version")
            if v and dev.online():
                out[d] = str(v)
        return out

    def _check_versions(self, now: float) -> None:
        """Alert once when the two devices run different agent code for
        longer than the grace (an update in progress is short)."""
        vs = self.versions()
        differ = len(set(vs.values())) > 1
        if not differ:
            if self._versions_alerted and self._notify and len(vs) > 1:
                asyncio.create_task(self._notify(
                    f"✅ Agent versions match again ({next(iter(vs.values()))})."))
            self._versions_differ_since = None
            self._versions_alerted = False
            return
        if self._versions_differ_since is None:
            self._versions_differ_since = now
        if (not self._versions_alerted
                and now - self._versions_differ_since >= self._version_grace_s):
            self._versions_alerted = True
            text = ", ".join(f"device {d} {v}" for d, v in vs.items())
            log.warning("agent versions differ: %s", text)
            if self._notify:
                asyncio.create_task(self._notify(
                    f"⚠ Agent versions differ: {text}. Run deploy\\cluster_update.py."))

    async def run(self) -> None:
        log.info("cluster state machine started (poll=%ss)", self._poll)
        while True:
            try:
                self.tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("cluster tick error: %s", exc)
            await asyncio.sleep(self._poll)

    def inputs(self, d: int) -> NodeInputs:
        dev = self._hub.device(d)
        svc = self._services[d]
        reading = self._health.plug_reading(d)
        return NodeInputs(
            control=dev.control is not None,
            seen=dev.online(),
            peer_seen=self._health.peer_sees(d),
            intentional_off=dev.intentional_offline,
            cut_pending=svc.vps.cut_pending,
            waking=time.time() < dev.suppress_deadman_until,
            socket=None if reading is None else bool(reading.get("socket")),
            watts=None if reading is None or reading.get("watts") is None
            else float(reading["watts"]),
            min_watts=getattr(svc.plug, "min_watts", 5.0),
        )

    def tick(self) -> dict[int, str]:
        out = {}
        for d in sorted(self._services):
            dev = self._hub.device(d)
            i = self.inputs(d)
            state = node_state(i)
            dev.peer_seen = i.peer_seen
            old = dev.node_state
            if state != old:
                dev.node_state = state
                dev.node_since = time.time()
                why = ", ".join(k for k in ("control", "seen", "peer_seen",
                                            "intentional_off", "cut_pending",
                                            "waking") if getattr(i, k))
                ev = {"ts": time.time(), "device": d, "from": old, "to": state,
                      "why": why or f"socket={i.socket} watts={i.watts}"}
                self.events.append(ev)
                log.info("device %s: %s -> %s (%s)", d, old, state, ev["why"])
            out[d] = state
        self._check_versions(time.time())
        return out

    def snapshot(self) -> dict:
        now = time.time()
        nodes = {}
        for d in sorted(self._services):
            dev = self._hub.device(d)
            nodes[d] = {"state": dev.node_state,
                        "for_s": round(now - dev.node_since) if dev.node_since else None,
                        "running": dev.running, "peer_seen": dev.peer_seen}
        vs = self.versions()
        return {"nodes": nodes, "events": list(self.events)[-20:],
                "versions": vs, "versions_match": len(set(vs.values())) <= 1}
