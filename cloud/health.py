"""health.py — heartbeats in, one health case per device out (cluster plan 1b).

Inputs every tick:
  - device beat: the agent's POST /status (every ~60 s) with uplink, AC,
    battery and its LAN view (router reachable, both plugs);
  - plug beat: each plug's MQTT state on the VPS (it connects to the broker
    itself, through the main router's Wi-Fi or the SMS box's), read every
    `poll` seconds;
  - home network: main router and SMS box state from both (homenet.py).

A device is missing after MISSING_S without a beat; a plug once neither a
live device sees it on the LAN nor has it been on the broker for
PLUG_MISSING_S.

Each tick publishes on the hub's DeviceState:
  health_case / health_text   the case (see classify)
  power_hold                  no idle shutdown and no plug cut while True
  deadman_ok                  the dead-man plug cycle may run (case 5 only)
and runs the case actions (7 cut plug, 8 plug on, 11 clean shutdown).
Alerts go out when an alert case starts and when it ends, at least
ALERT_GAP_S apart per device (case 14, flapping).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from homenet import NetWatch, assess, plug_line
from hub import Hub

log = logging.getLogger("health")

Notify = Callable[[str], Awaitable[None]]

MISSING_S = 180.0
PLUG_MISSING_S = 180.0
ALERT_GAP_S = 600.0
WARMUP_S = 180.0
BATTERY_SHUTDOWN_PCT = 15.0

PEER_FRESH_S = 90.0
HINT_EVERY_S = 300.0

HOLD_CASES = {"3", "4", "4b", "9", "10", "11"}
# 4/4b are announced by the plug warnings (homenet), 3 by the router warning
ALERT_CASES = {"9", "10", "11"}
ROUTER_OK = {"ok": True, "down": False, "no_internet": False}

CASE_TEXT = {
    "0": "waiting for data",
    "1": "normal",
    "2": "SMS backup down (router fine)",
    "3": "home router down, running on SMS",
    "4": "plug not answering (on AC)",
    "4b": "plug not answering, running on battery",
    "5": "device silent while its plug draws power (hung?)",
    "6": "device off",
    "7": "device off but its plug is on",
    "8": "turned on by itself, plug off",
    "9": "no link to the VPS, but the other device sees it at home",
    "10": "device and its plug both silent (home internet or power down?)",
    "11": "house power out: router down, on battery",
}


@dataclass
class Inputs:
    seen: bool
    intentional_off: bool = False
    waking: bool = False
    plug_link: str = "unknown"           # ok | missing | unknown
    socket: bool | None = None
    watts: float | None = None
    router_ok: bool | None = None
    sms_ok: bool | None = None
    ac: bool | None = None
    battery: float | None = None
    min_watts: float = 5.0
    peer_sees: bool = False


@dataclass
class Verdict:
    case: str
    hold: bool = False
    deadman_ok: bool = False
    action: str | None = None            # cut_plug | plug_on | shutdown

    @property
    def text(self) -> str:
        return CASE_TEXT.get(self.case, "?")


def classify(i: Inputs) -> Verdict:
    """The case table from the cluster plan, for one device."""
    if i.seen:
        if i.router_ok is False:
            if i.ac is False:
                low = i.battery is not None and i.battery <= BATTERY_SHUTDOWN_PCT
                return Verdict("11", hold=True, action="shutdown" if low else None)
            return Verdict("3", hold=True)
        if i.plug_link == "missing":
            return Verdict("4b" if i.ac is False else "4", hold=True)
        if i.ac is False and i.socket is False:
            return Verdict("8", action="plug_on")
        if i.sms_ok is False:
            return Verdict("2")
        return Verdict("1")

    if i.peer_sees:
        return Verdict("9", hold=True)
    if i.waking:
        return Verdict("6")
    if i.socket is None:
        if i.plug_link == "missing":
            return Verdict("10", hold=True)
        return Verdict("0")
    if i.socket is False:
        return Verdict("6")
    if i.watts is not None and i.watts < i.min_watts:
        return Verdict("7", action="cut_plug")
    return Verdict("5", deadman_ok=not i.intentional_off)


class PlugWatch:
    """the VPS's MQTT view of each plug, read every `poll` seconds."""

    def __init__(self, plugs: dict, poll: float = 60.0) -> None:
        self._plugs = plugs                  # {device_id: SmartPlug}
        self._poll = poll
        self.views: dict[int, dict] = {}
        self._silent_since: dict[int, float] = {}

    async def run(self) -> None:
        while True:
            for n, plug in self._plugs.items():
                if plug.configured:
                    await self.poll_one(n, plug)
            await asyncio.sleep(self._poll)

    async def poll_one(self, n: int, plug) -> None:
        try:
            st = await plug.link_status()
        except Exception as exc:                     # noqa: BLE001
            st = {"ok": False, "error": str(exc)}
        now = time.time()
        if st.get("ok") and st.get("online"):
            self._silent_since.pop(n, None)
        else:
            self._silent_since.setdefault(n, now)
        self.views[n] = {**st, "ts": now}

    def state(self, n: int) -> str:
        if n not in self.views:
            return "unknown"
        since = self._silent_since.get(n)
        if since is None:
            return "ok"
        return "missing" if time.time() - since >= PLUG_MISSING_S else "ok"

    def online(self, n: int) -> bool:
        """The last read had plug n on the broker."""
        v = self.views.get(n)
        return bool(v and v.get("ok") and v.get("online"))

    def reading(self, n: int) -> dict | None:
        """A broker view recent enough to trust socket and watts."""
        v = self.views.get(n)
        if (v and v.get("ok") and v.get("online")
                and time.time() - v["ts"] <= self._poll + 90.0):
            return v
        return None


class Health:
    def __init__(self, hub: Hub, services: dict, plugwatch: PlugWatch,
                 notify: Notify, *, poll: float = 30.0,
                 act: bool = True) -> None:
        self._hub = hub
        self._services = services            # {device_id: DeviceServices}
        self._pw = plugwatch
        self._notify = notify
        self._poll = poll
        self._act = act
        self._started = time.time()
        self._streak: dict[int, tuple[str, int]] = {}
        self._acted: dict[int, str] = {}
        self._announced: dict[int, str | None] = {}
        self._last_alert: dict[int, float] = {}
        self._hinted: dict[int, tuple[str | None, float]] = {}
        self._drills: dict[tuple[int, str], float] = {}
        self.net = NetWatch(notify)

    # ── drills (deploy/cluster_drill.py) ──────────────────────────────────
    DRILLS = ("plug_missing", "sms_off")

    def drill(self, d: int, what: str, for_s: float) -> dict:
        """Pretend one input is bad for a while; 'clear' ends every drill."""
        if what == "clear":
            for k in [k for k in self._drills if k[0] == d]:
                self._drills.pop(k)
        elif what in self.DRILLS:
            self._drills[(d, what)] = time.time() + max(10.0, min(for_s, 900.0))
        else:
            return {"ok": False, "error": f"drill must be {', '.join(self.DRILLS)} or clear"}
        return {"ok": True, "drills": self.drills(d)}

    def drills(self, d: int) -> dict[str, int]:
        now = time.time()
        return {w: int(u - now) for (n, w), u in self._drills.items() if n == d and u > now}

    def _drilling(self, d: int, what: str) -> bool:
        return self._drills.get((d, what), 0.0) > time.time()

    async def run(self) -> None:
        log.info("health watch started (poll=%ss, act=%s)", self._poll, self._act)
        while True:
            try:
                await self.tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("health tick error: %s", exc)
            await asyncio.sleep(self._poll)

    # ── inputs ────────────────────────────────────────────────────────────

    def _seen(self, d: int) -> bool:
        return self._hub.device(d).seconds_since_status() <= MISSING_S

    def _home(self, d: int) -> dict:
        return self._hub.device(d).telemetry().get("home") or {}

    def _sms_status(self, d: int) -> str | None:
        if self._drilling(d, "sms_off"):
            return "off"
        return (self._hub.device(d).telemetry().get("devices") or {}).get("sms")

    def _laptop_views(self) -> list[dict]:
        out = []
        for d in sorted(self._services):
            seen = self._seen(d)
            tele = self._hub.device(d).telemetry() if seen else {}
            home = tele.get("home") or {}
            out.append({
                "label": self._hub.device(d).label, "seen": seen,
                "net": tele.get("net"), "internet": tele.get("internet"),
                "router_ok": home.get("router_ok"),
                "wifi": (tele.get("devices") or {}).get("wifi"),
                "sms": self._sms_status(d) if seen else None,
                "sees_plugs": [int(n) for n, p in (home.get("plugs") or {}).items()
                               if p.get("ok") and str(n).isdigit()],
            })
        return out

    def _lan_by(self, n: int) -> str | None:
        for d in sorted(self._services):
            if self._seen(d) and ((self._home(d).get("plugs") or {}).get(str(n)) or {}).get("ok"):
                return self._hub.device(d).label
        return None

    def plug_label(self, n: int) -> str:
        return f"plug {n} ({self._hub.device(n).label})"

    def _plug_views(self) -> list[dict]:
        out = []
        for n in sorted(self._services):
            v = self._pw.views.get(n)
            out.append({"n": n, "label": self.plug_label(n),
                        "known": bool(v and v.get("ok")),
                        "online": self._pw.online(n), "wifi": (v or {}).get("wifi"),
                        "view": v, "lan_by": self._lan_by(n)})
        return out

    def plug_lines(self) -> list[str]:
        return [plug_line(self.plug_label(n), self._pw.views.get(n))
                for n in sorted(self._services)]

    def plug_reading(self, n: int) -> dict | None:
        """Fresh broker reading first, then any live device's LAN view of plug n."""
        return self._pw.reading(n) or self.lan_reading(n)

    def plug_state(self, n: int) -> str:
        """ok while a live device sees plug n on the LAN, else the broker's view."""
        return "ok" if self.lan_reading(n) is not None else self._pw.state(n)

    def lan_reading(self, n: int) -> dict | None:
        for d in sorted(self._services):
            if not self._seen(d):
                continue
            lan = (self._home(d).get("plugs") or {}).get(str(n)) or {}
            if lan.get("ok"):
                return lan
        return None

    def inputs(self, d: int, router_ok: bool | None) -> Inputs:
        dev = self._hub.device(d)
        tele = dev.telemetry()
        seen = self._seen(d)
        reading = self.plug_reading(d)
        plug = self._services[d].plug
        sms = self._sms_status(d) if seen else None
        return Inputs(
            seen=seen,
            intentional_off=dev.intentional_offline,
            waking=time.time() < dev.suppress_deadman_until,
            plug_link="missing" if self._drilling(d, "plug_missing") else self.plug_state(d),
            socket=None if reading is None else bool(reading.get("socket")),
            watts=None if reading is None or reading.get("watts") is None
            else float(reading["watts"]),
            router_ok=router_ok,
            sms_ok={"on": True, "off": False}.get(sms) if seen else None,
            ac=tele.get("ac_connected") if seen else None,
            battery=tele.get("battery_pct") if seen else None,
            min_watts=getattr(plug, "min_watts", 5.0),
            peer_sees=not seen and self.peer_sees(d),
        )

    def _peer(self, d: int) -> dict:
        return self._hub.device(d).telemetry().get("peer") or {}

    def peer_sees(self, d: int) -> bool:
        """Another live device had a peer frame from device d within
        PEER_FRESH_S (its beat age counts: beats are ~60 s apart)."""
        for o in sorted(self._services):
            if o == d or not self._seen(o):
                continue
            p = self._peer(o)
            age = p.get("last_seen_s")
            if age is None:
                continue
            age += self._hub.device(o).seconds_since_status()
            if (p.get("connected") and p.get("peer_device") == d
                    and age <= PEER_FRESH_S):
                return True
        return False

    async def _push_hints(self) -> None:
        """Tell each live device the other's LAN address (peer discovery)."""
        now = time.time()
        for d in sorted(self._services):
            for o in sorted(self._services):
                if o == d or not (self._seen(d) and self._seen(o)):
                    continue
                if self._peer(d).get("connected"):
                    continue
                ip = self._peer(o).get("lan_ip")
                last_ip, last_at = self._hinted.get(d, (None, 0.0))
                if not ip or (ip == last_ip and now - last_at < HINT_EVERY_S):
                    continue
                self._hinted[d] = (ip, now)
                try:
                    await self._hub.send_command(d, "action", "peer",
                                                 {"op": "hint", "host": ip},
                                                 timeout=15)
                except Exception as exc:             # noqa: BLE001
                    log.info("peer hint to %s failed: %s", d, exc)

    # ── tick ──────────────────────────────────────────────────────────────

    async def tick(self) -> dict[int, Verdict]:
        plugs = self._plug_views()
        view = assess(self._laptop_views(), plugs)
        router_ok = ROUTER_OK.get(view["router"])
        verdicts = {d: classify(self.inputs(d, router_ok)) for d in sorted(self._services)}
        seen_uplinks = [self._hub.device(d).telemetry().get("net")
                        for d in verdicts if self._seen(d)]
        both_sms = len(seen_uplinks) == 2 and all(u == "sms" for u in seen_uplinks)
        warm = time.time() - self._started >= WARMUP_S
        await self.net.update(view, plugs, announce=warm)
        for d, v in verdicts.items():
            dev = self._hub.device(d)
            if dev.health_case != v.case:
                dev.health_since = time.time()
            dev.health_case = v.case
            dev.health_text = v.text + (" · both devices on SMS" if both_sms else "")
            dev.power_hold = v.hold
            dev.deadman_ok = v.deadman_ok
            prev_case, n = self._streak.get(d, ("", 0))
            self._streak[d] = (v.case, n + 1 if prev_case == v.case else 1)
            if v.case != prev_case:
                self._acted.pop(d, None)
            if warm and self._act:
                await self._do_action(d, v)
                await self._alert(d, v, verdicts)
        await self._push_hints()
        return verdicts

    async def _do_action(self, d: int, v: Verdict) -> None:
        if v.action is None or self._acted.get(d) == v.case:
            return
        svc = self._services[d]
        dev = self._hub.device(d)
        _, ticks = self._streak[d]
        if v.action == "cut_plug":
            if ticks < 6 or svc.vps.cut_pending:
                return
            self._acted[d] = v.case
            res = await svc.plug.off()
            await self._notify(
                f"🔌 {dev.label}: off, but its plug was on with no draw — "
                + ("plug switched off." if res.get("ok")
                   else f"plug off FAILED: {res.get('error', '?')}"))
        elif v.action == "plug_on":
            if ticks < 2:
                return
            self._acted[d] = v.case
            res = await svc.plug.on()
            await self._notify(
                f"🔌 {dev.label}: running on battery with its plug off — "
                + ("plug switched on." if res.get("ok")
                   else f"plug on FAILED: {res.get('error', '?')}"))
        elif v.action == "shutdown":
            self._acted[d] = v.case
            await self._notify(
                f"🪫 {dev.label}: house power out and battery at "
                f"{dev.telemetry().get('battery_pct')}% — shutting down cleanly.")
            await svc.vps.offline()

    async def _alert(self, d: int, v: Verdict, verdicts: dict[int, Verdict]) -> None:
        want = v.case if v.case in ALERT_CASES else None
        had = self._announced.get(d)
        if want == had:
            return
        now = time.time()
        if now - self._last_alert.get(d, 0.0) < ALERT_GAP_S:
            return
        self._announced[d] = want
        self._last_alert[d] = now
        label = self._hub.device(d).label
        if self.drills(d):
            label = "[DRILL] " + label
        if want == "10" and any(self._announced.get(o) == "10"
                                for o in verdicts if o != d):
            return
        if want is None:
            await self._notify(f"✅ {label}: back to {v.text} (case {v.case}).")
        else:
            await self._notify(f"⚠️ {label}: case {v.case}, {v.text}. "
                               "Power rules on hold until it clears.")

    def snapshot(self, d: int) -> dict:
        dev = self._hub.device(d)
        return {"case": dev.health_case, "text": dev.health_text,
                "hold": dev.power_hold, "since": dev.health_since,
                "plug_state": self.plug_state(d),
                "plug_link": self._pw.state(d),
                "plug": self._pw.views.get(d),
                "drills": self.drills(d)}
