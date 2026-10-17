"""Offline checks for power.py (OpenBeken plugs over MQTT). Run: python test_power.py"""
import asyncio
import sys

import power  # noqa: E402
from power import MqttLink, SmartPlug  # noqa: E402

power.CONFIRM_S = 0.6
fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


class FakeLink(MqttLink):
    """Broker stand-in: a published PowerN command is echoed back like a
    real OpenBeken plug does (unless the plug is 'deaf')."""

    def __init__(self):
        super().__init__()
        self.up = True
        self.sent = []
        self.deaf = False

    def publish(self, topic, payload):
        if not self.up:
            return False
        self.sent.append((topic, payload))
        t, cmd = topic.split("/")[1], topic.split("/")[2]
        ch = cmd.removeprefix("Power") or "1"
        if not self.deaf:
            asyncio.get_running_loop().call_later(
                0.05, self.handle, f"{t}/{ch}/get", "1" if payload == "On" else "0")
        return True


def make(home_answers=None):
    link = FakeLink()
    log = []

    async def home(n, op):
        log.append((n, op))
        ans = (home_answers or {}).get(op)
        return None if ans is None else {**ans, "via": 1}

    p = SmartPlug(link, "plug2", number=2, home=home, cycle_wait=0,
                  wifi_main="HOME", wifi_backup="SMSBOX")
    return p, link, log


async def main():
    # topic parsing
    link = MqttLink()
    st = link.watch("plug1")
    link.handle("plug1/connected", "online")
    link.handle("plug1/1/get", "1")
    link.handle("plug1/2/get", "0")
    link.handle("plug1/power/get", "23.46")
    link.handle("plug1/voltage/get", "229.9")
    check("connected", st.connected, True)
    check("relays", st.relays, {1: True, 2: False})
    check("watts", st.watts, 23.5)
    check("volts", st.volts, 229.9)
    link.handle("stat/plug1/RESULT", '{"POWER1":"OFF","POWER2":"ON"}')
    check("stat RESULT", st.relays, {1: False, 2: True})
    link.handle("stat/plug1/POWER1", "ON")
    check("stat POWER1", st.relays[1], True)
    link.handle("tele/plug1/STATE", '{"POWER1":"ON","Wifi":{"SSId":"SMSBOX","Signal":-61}}')
    check("tele STATE wifi", (st.ssid, st.signal), ("SMSBOX", -61))
    link.handle("tele/plug1/SENSOR",
                '{"ENERGY":{"Power":4.2,"Voltage":231,"ConsumptionTotal":1.2345}}')
    check("tele SENSOR", (st.watts, st.volts, st.kwh), (4.2, 231.0, 1.234))
    seen = st.last_seen
    link.handle("plug1/connected", "offline")
    check("LWT offline", st.connected, False)
    check("LWT does not count as seen", st.last_seen, seen)
    link.handle("other/1/get", "1")
    check("unknown topic ignored", "other" in link.states, False)
    link.handle("tele/plug1/SENSOR", "not json")
    check("bad SENSOR kept watts", st.watts, 4.2)

    # switching over MQTT, no laptop needed
    p, link, log = make()
    link.handle("plug2/connected", "online")
    r = await p.on()
    check("on via mqtt", (r.get("ok"), r.get("path")), (True, "mqtt"))
    check("command topic", link.sent[-1], ("cmnd/plug2/Power1", "On"))
    check("home not asked", log, [])
    r = await p.cycle()
    check("cycle ok", (r.get("ok"), r.get("path")), (True, "mqtt/mqtt"))
    check("cycle sent off then on", [s[1] for s in link.sent[-2:]], ["Off", "On"])
    r = await p.lamp(True)
    check("lamp on", (r, link.sent[-1]), ({"ok": True, "lamp": True}, ("cmnd/plug2/Power2", "On")))
    check("lamp did not touch the socket", p._st.relays[1], True)
    check("no brightness", (await p.brightness(50)).get("ok"), False)

    # plug not on the broker -> a laptop switches it over the LAN
    p, link, log = make({"off": {"ok": True}})
    r = await p.off()
    check("offline plug: off via home", (r.get("ok"), r.get("path")), (True, "lan"))
    check("nothing published", link.sent, [])
    check("home asked once", log, [(2, "off")])

    # plug on the broker but never confirms -> home fallback
    p, link, log = make({"on": {"ok": True}})
    link.handle("plug2/connected", "online")
    link.deaf = True
    r = await p.on()
    check("no confirm: home used", (r.get("ok"), r.get("path")), (True, "lan"))

    # neither works -> error, not a fake ok
    p, link, log = make()
    r = await p.on()
    check("all down: error", (r.get("ok"), r.get("error")),
          (False, "plug not connected to the VPS"))

    # stale: connected but silent for longer than STALE_S
    p, link, _ = make()
    link.handle("plug2/connected", "online")
    p._st.last_seen -= power.STALE_S + 1
    check("stale plug is off the broker", p.on_broker(), False)

    # status: broker state first, no laptop call
    p, link, log = make({"status": {"ok": True, "online": True, "socket": True}})
    link.handle("plug2/connected", "online")
    link.handle("plug2/1/get", "1")
    link.handle("plug2/power/get", "12")
    link.handle("tele/plug2/STATE", '{"Wifi":{"SSId":"HOME","Signal":-50}}')
    r = await p.status()
    check("status via mqtt", (r.get("path"), r.get("socket"), r.get("watts"),
                              r.get("laptop_running"), r.get("wifi"), r.get("signal")),
          ("mqtt", True, 12.0, True, "main", -50))
    check("status did not ask home", log, [])
    link.handle("tele/plug2/STATE", '{"Wifi":{"SSId":"SMSBOX","Signal":-70}}')
    check("backup wifi", p.wifi(), "backup")
    link.handle("tele/plug2/STATE", '{"Wifi":{"SSId":"Neighbour"}}')
    check("other wifi is the SSID", p.wifi(), "Neighbour")
    link.handle("plug2/connected", "offline")
    r = await p.status()
    check("plug off broker: status via home", r.get("path"), "lan")
    r = await p.link_status()
    check("link_status offline", (r.get("ok"), r.get("online")), (True, False))
    link.up = False
    r = await p.link_status()
    check("broker down: link_status error", r.get("ok"), False)

    # no watts published -> no watts key (callers treat it as unknown)
    p, link, _ = make()
    link.handle("plug2/connected", "online")
    link.handle("plug2/1/get", "0")
    r = await p.link_status()
    check("no watts key", "watts" in r, False)


asyncio.run(main())
print("\nall ok" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
