"""power.py — the two OpenBeken smart plugs, for the gate, bot and watchdogs.

Each plug connects to the Mosquitto broker on the VPS by itself (through the
main router's Wi-Fi, or the SMS box's Wi-Fi as its backup), so the VPS can read
and switch it with no laptop up. When a plug is not on the broker, on/off/
cycle/status fall back to a laptop at home that reaches it over the LAN.

.env (gate):
  MQTT_HOST, MQTT_PORT      broker, default 127.0.0.1:1883
  MQTT_USER, MQTT_PASS      the gate's broker login
  PLUGn_MQTT_TOPIC          plug n's OpenBeken client topic, default plugN
  PLUG_RELAY_CH             socket channel, default 1
  PLUG_LAMP_CH              night-lamp channel, default 2
  PLUG_WIFI_MAIN            main router SSID (to tell main from backup Wi-Fi)
  PLUG_WIFI_BACKUP          SMS box SSID
  PLUG_MIN_WATTS            below = nothing drawing, default 5
  PLUG_CYCLE_WAIT           seconds off during a cycle, default 10

Topics read: <t>/connected (LWT), <t>/<ch>/get, <t>/power|voltage|current/get,
tele/<t>/STATE (relays + Wi-Fi), tele/<t>/SENSOR, stat/<t>/RESULT|POWERn.
Commands: cmnd/<t>/Power<ch> On|Off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("power")

CONFIRM_S = 8.0
STALE_S = 180.0          # plugs publish every 60 s; older = not on the broker

# home(plug_number, op) -> the node's result dict, or None if no node could.
HomeHook = Callable[[int, str], Awaitable[Optional[dict]]]


class PlugState:
    def __init__(self) -> None:
        self.connected: bool | None = None
        self.relays: dict[int, bool] = {}
        self.watts: float | None = None
        self.volts: float | None = None
        self.amps: float | None = None
        self.kwh: float | None = None
        self.ssid: str | None = None
        self.signal: int | None = None
        self.last_seen = 0.0


class MqttLink:
    """One broker connection shared by both plugs."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1883,
                 user: str = "", password: str = "") -> None:
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._client = None
        self._lock = threading.Lock()
        self.states: dict[str, PlugState] = {}
        self.up = False

    def watch(self, topic: str) -> PlugState:
        with self._lock:
            return self.states.setdefault(topic, PlugState())

    def start(self) -> None:
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="localserver-gate")
        if self._user:
            c.username_pw_set(self._user, self._password)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 30)
        c.connect_async(self._host, self._port, keepalive=30)
        c.loop_start()
        self._client = c

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason, props=None) -> None:
        self.up = not getattr(reason, "is_failure", False)
        log.info("mqtt connected (%s)", reason)
        for t in list(self.states):
            for sub in (f"{t}/#", f"tele/{t}/#", f"stat/{t}/#"):
                client.subscribe(sub, qos=1)

    def _on_disconnect(self, client, userdata, flags, reason, props=None) -> None:
        self.up = False
        log.warning("mqtt disconnected (%s)", reason)

    def _on_message(self, client, userdata, msg) -> None:
        self.handle(msg.topic, msg.payload.decode("utf-8", "replace"))

    def handle(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        if parts[0] in ("tele", "stat") and len(parts) >= 3:
            prefix, t, leaf = parts[0], parts[1], "/".join(parts[2:])
        else:
            prefix, t, leaf = "", parts[0], "/".join(parts[1:])
        with self._lock:
            st = self.states.get(t)
            if st is None:
                return
            p = payload.strip()
            if not prefix:
                self._plain(st, leaf, p)
            elif prefix == "stat" and leaf.upper().startswith("POWER"):
                _relay(st, leaf, p)
            elif prefix in ("stat", "tele"):
                self._json(st, leaf, p)
            if not (not prefix and leaf == "connected" and not st.connected):
                st.last_seen = time.time()

    @staticmethod
    def _plain(st: PlugState, leaf: str, p: str) -> None:
        if leaf == "connected":
            st.connected = p.lower() == "online"
            return
        head, _, tail = leaf.partition("/")
        if tail != "get":
            return
        if head.isdigit():
            st.relays[int(head)] = p not in ("0", "", "OFF", "off")
        elif head == "power":
            st.watts = _num(p)
        elif head == "voltage":
            st.volts = _num(p)
        elif head == "current":
            st.amps = _num(p, 3)

    @staticmethod
    def _json(st: PlugState, leaf: str, p: str) -> None:
        try:
            data = json.loads(p)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        for k, v in data.items():
            if k.upper().startswith("POWER"):
                _relay(st, k, str(v))
        wifi = data.get("Wifi")
        if isinstance(wifi, dict):
            st.ssid = wifi.get("SSId") or st.ssid
            if isinstance(wifi.get("Signal"), (int, float)):
                st.signal = int(wifi["Signal"])
        energy = data.get("ENERGY")
        if isinstance(energy, dict):
            for key, attr, nd in (("Power", "watts", 1), ("Voltage", "volts", 1),
                                  ("Current", "amps", 3), ("ConsumptionTotal", "kwh", 3)):
                if energy.get(key) is not None:
                    setattr(st, attr, _num(energy[key], nd))

    def publish(self, topic: str, payload: str) -> bool:
        if self._client is None or not self.up:
            return False
        return self._client.publish(topic, payload, qos=1).rc == 0


def _relay(st: PlugState, key: str, value: str) -> None:
    ch = key.upper().removeprefix("POWER") or "1"
    if ch.isdigit():
        st.relays[int(ch)] = value.strip().upper() in ("ON", "1")


def _num(v, nd: int = 1) -> float | None:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


class SmartPlug:
    """One OpenBeken plug: socket (relay), night lamp, energy meter."""

    def __init__(self, link: MqttLink, topic: str, *, number: int,
                 home: HomeHook | None = None, relay_ch: int = 1,
                 lamp_ch: int = 2, min_watts: float = 5.0,
                 cycle_wait: float = 10.0, wifi_main: str = "",
                 wifi_backup: str = "") -> None:
        self._link = link
        self.topic = topic
        self.number = number
        self.home = home
        self._relay = relay_ch
        self._lamp = lamp_ch
        self.min_watts = min_watts
        self._cycle_wait = cycle_wait
        self._wifi_main = wifi_main
        self._wifi_backup = wifi_backup
        self._st = link.watch(topic)
        self.last_state: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    def on_broker(self) -> bool:
        st = self._st
        return bool(self._link.up and st.connected
                    and time.time() - st.last_seen <= STALE_S)

    def wifi(self) -> str | None:
        """main | backup | the SSID, None if not reported yet."""
        ssid = self._st.ssid
        if not ssid:
            return None
        if ssid == self._wifi_main:
            return "main"
        if ssid == self._wifi_backup:
            return "backup"
        return ssid

    async def _via_home(self, op: str) -> dict | None:
        if self.home is None:
            return None
        try:
            r = await self.home(self.number, op)
        except Exception as exc:                    # noqa: BLE001
            log.warning("plug %s %s via home failed: %s", self.number, op, exc)
            return None
        return r if r and r.get("ok") else None

    async def _mqtt_set(self, ch: int, on: bool) -> dict:
        state = "on" if on else "off"
        if not self.on_broker():
            return {"ok": False, "error": "plug not connected to the VPS"}
        if not self._link.publish(f"cmnd/{self.topic}/Power{ch}", "On" if on else "Off"):
            return {"ok": False, "error": "mqtt publish failed"}
        deadline = time.time() + CONFIRM_S
        while time.time() < deadline:
            if self._st.relays.get(ch) is on:
                return {"ok": True, "state": state, "path": "mqtt"}
            await asyncio.sleep(0.2)
        return {"ok": False, "error": f"plug did not confirm {state}"}

    async def _switch(self, on: bool) -> dict:
        r = await self._mqtt_set(self._relay, on)
        if not r.get("ok"):
            home = await self._via_home("on" if on else "off")
            if home is not None:
                r = {"ok": True, "state": "on" if on else "off", "path": "lan",
                     "via": home.get("via")}
        if r.get("ok"):
            self.last_state = r["state"]
        return r

    async def on(self) -> dict:
        return await self._switch(True)

    async def off(self) -> dict:
        return await self._switch(False)

    async def cycle(self) -> dict:
        off = await self._switch(False)
        if not off.get("ok"):
            return off
        await asyncio.sleep(self._cycle_wait)
        on = await self._switch(True)
        if not on.get("ok"):
            log.error("plug cycle: off worked but on failed: %s", on.get("error"))
            return on
        return {"ok": True, "state": "on", "cycled": True,
                "path": f"{off.get('path')}/{on.get('path')}"}

    async def link_status(self) -> dict:
        """What the plug last told the VPS over MQTT (no network call)."""
        if not self._link.up:
            return {"ok": False, "error": "mqtt broker not connected"}
        st = self._st
        seen = round(time.time() - st.last_seen) if st.last_seen else None
        if not self.on_broker():
            return {"ok": True, "online": False, "seen_s": seen}
        socket = bool(st.relays.get(self._relay))
        out = {"ok": True, "online": True, "socket": socket,
               "lamp": st.relays.get(self._lamp), "volts": st.volts,
               "amps": st.amps, "kwh": st.kwh, "wifi": self.wifi(),
               "ssid": st.ssid, "signal": st.signal, "seen_s": seen}
        if st.watts is not None:
            out.update(watts=st.watts, drawing=st.watts > self.min_watts,
                       laptop_running=socket and st.watts > self.min_watts)
        return out

    async def status(self) -> dict:
        st = await self.link_status()
        if st.get("ok") and st.get("online"):
            return {**st, "path": "mqtt"}
        lan = await self._via_home("status")
        if lan is not None:
            return {**{k: v for k, v in lan.items() if k not in ("type", "id")},
                    "path": "lan"}
        return st

    async def lamp(self, on: bool) -> dict:
        r = await self._mqtt_set(self._lamp, on)
        return {"ok": True, "lamp": on} if r.get("ok") else r

    async def brightness(self, pct: int) -> dict:
        return {"ok": False, "error": "the lamp is on/off only"}
