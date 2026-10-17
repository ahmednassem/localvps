"""Offline checks for health.py. Run: python test_health.py"""
import asyncio
import sys

import health
import homenet
from health import Inputs, classify

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


def case(**kw):
    v = classify(Inputs(**kw))
    return (v.case, v.hold, v.deadman_ok, v.action)


up = dict(seen=True, plug_link="ok", socket=True, watts=12.0, router_ok=True,
          sms_ok=True, ac=True, battery=90)
check("1 normal", case(**up), ("1", False, False, None))
check("2 sms down", case(**{**up, "sms_ok": False}), ("2", False, False, None))
check("3 router down", case(**{**up, "router_ok": False, "plug_link": "missing"}),
      ("3", True, False, None))
check("4 plug missing on AC", case(**{**up, "plug_link": "missing"}), ("4", True, False, None))
check("4b plug missing on battery", case(**{**up, "plug_link": "missing", "ac": False}),
      ("4b", True, False, None))
check("8 self-boot plug off", case(**{**up, "ac": False, "socket": False}),
      ("8", False, False, "plug_on"))
check("11 power out", case(**{**up, "router_ok": False, "ac": False, "battery": 60}),
      ("11", True, False, None))
check("11 low battery -> shutdown",
      case(**{**up, "router_ok": False, "ac": False, "battery": 14})[3], "shutdown")

down = dict(seen=False, plug_link="ok", router_ok=True)
check("5 hung", case(**down, socket=True, watts=20.0), ("5", False, True, None))
check("5 but manual offline: no dead-man",
      case(**down, socket=True, watts=20.0, intentional_off=True)[2], False)
check("6 off", case(**down, socket=False, watts=0.0), ("6", False, False, None))
check("6 while waking", case(**down, socket=True, watts=0.5, waking=True)[0], "6")
check("7 plug on, no draw", case(**down, socket=True, watts=0.4),
      ("7", False, False, "cut_plug"))
check("10 all silent", case(seen=False, plug_link="missing"), ("10", True, False, None))
check("0 no data yet", case(seen=False)[0], "0")


class Tele:
    def __init__(self):
        self.t = 10_000.0


clock = Tele()
health.time.time = lambda: clock.t
homenet.time.time = lambda: clock.t


class Dev:
    def __init__(self, i):
        self.id, self.label = i, f"laptop{i}"
        self.last = clock.t
        self.tele = {"net": "wifi", "internet": True, "ac_connected": True,
                     "battery_pct": 90, "devices": {"sms": "on"}}
        self.intentional_offline = False
        self.suppress_deadman_until = 0.0
        self.health_case = None
        self.health_text = ""
        self.health_since = 0.0
        self.power_hold = False
        self.deadman_ok = None

    def seconds_since_status(self):
        return clock.t - self.last

    def telemetry(self):
        return self.tele


class Hub:
    def __init__(self):
        self.devs = {1: Dev(1), 2: Dev(2)}
        self.sent = []

    def device(self, d):
        return self.devs[d]

    async def send_command(self, d, kind, name, args=None, timeout=30.0):
        self.sent.append((d, args.get("host")))
        return {"ok": True}


class Plug:
    configured = True
    min_watts = 5.0

    def __init__(self):
        self.online, self.socket, self.watts = True, True, 12.0
        self.wifi = "main"
        self.ons = self.offs = 0

    async def link_status(self):
        return {"ok": True, "online": self.online, "socket": self.socket, "watts": self.watts,
                "wifi": self.wifi}

    async def on(self):
        self.ons += 1
        self.socket = True
        return {"ok": True}

    async def off(self):
        self.offs += 1
        self.socket = False
        return {"ok": True}


class Vps:
    cut_pending = False

    def __init__(self):
        self.offlines = 0

    async def offline(self):
        self.offlines += 1
        return {"ok": True}


class Svc:
    def __init__(self):
        self.plug, self.vps = Plug(), Vps()


async def main():
    hub = Hub()
    svcs = {1: Svc(), 2: Svc()}
    pw = health.PlugWatch({d: s.plug for d, s in svcs.items()})
    msgs = []

    async def note(m):
        msgs.append(m)

    h = health.Health(hub, svcs, pw, note)

    async def step(seconds=30):
        clock.t += seconds
        for d, dev in hub.devs.items():
            if dev.tele is not None and getattr(dev, "beating", True):
                dev.last = clock.t
        for n, s in svcs.items():
            await pw.poll_one(n, s.plug)
        return await h.tick()

    v = await step()
    check("start: normal", (v[1].case, v[2].case), ("1", "1"))
    check("hub gets the case", hub.devs[1].health_case, "1")

    hub.devs[2].beating = False
    svcs[2].plug.watts = 0.3
    for _ in range(7):
        await step()
    check("device 2 missing, plug on, no draw -> case 7", hub.devs[2].health_case, "7")
    check("case 7 waits 3 min before cutting", svcs[2].plug.offs, 0)
    for _ in range(8):
        await step()
    check("case 7 cut the plug once", svcs[2].plug.offs, 1)
    check("then case 6", hub.devs[2].health_case, "6")
    check("dead-man not allowed when off", hub.devs[2].deadman_ok, False)

    svcs[1].plug.online = False
    svcs[2].plug.online = False
    for _ in range(12):
        await step()
    check("both plugs silent 6 min, device 1 on wifi -> case 4", hub.devs[1].health_case, "4")
    check("hold on", hub.devs[1].power_hold, True)
    check("no case-4 alert (the plug warning covers it)",
          any("case 4" in m for m in msgs), False)
    check("plug 1 offline warning", any(m.startswith("🔌 plug 1 (laptop1) is OFFLINE")
                                        for m in msgs), True)
    check("device 2: silent and plug silent -> 10", hub.devs[2].health_case, "10")

    n_before = len(msgs)
    svcs[1].plug.online = True
    for _ in range(2):
        await step()
    check("cleared, but inside 10 min -> no alert yet", len(msgs) == n_before
          or not any("back to" in m for m in msgs[n_before:]), True)
    for _ in range(20):
        await step()
    check("plug 1 back online warning",
          any(m.startswith("✅ plug 1 (laptop1) back online") for m in msgs), True)

    hub.devs[1].tele = {**hub.devs[1].tele, "ac_connected": False}
    svcs[1].plug.socket = False
    for _ in range(3):
        await step()
    check("battery with plug off -> plug on", svcs[1].plug.ons, 1)

    hub.devs[1].tele = {**hub.devs[1].tele, "ac_connected": True,
                        "peer": {"connected": True, "peer_device": 2,
                                 "last_seen_s": 3, "lan_ip": "192.168.0.5"}}
    svcs[2].plug.online, svcs[2].plug.socket, svcs[2].plug.watts = True, True, 20.0
    for _ in range(2):
        await step()
    check("silent at the VPS but peer sees it -> case 9", hub.devs[2].health_case, "9")
    check("case 9 holds power rules", hub.devs[2].power_hold, True)
    check("case 9: no dead-man", hub.devs[2].deadman_ok, False)
    hub.devs[1].tele["peer"] = {**hub.devs[1].tele["peer"], "last_seen_s": 120}
    await step()
    check("stale peer view -> case 5 again", hub.devs[2].health_case, "5")

    hub.devs[2].beating = True
    hub.devs[2].last = clock.t
    hub.devs[1].tele["peer"] = {"connected": False, "lan_ip": "192.168.0.5"}
    hub.devs[2].tele = {**hub.devs[2].tele,
                        "peer": {"connected": False, "lan_ip": "192.168.0.2"}}
    await step()
    await step()
    check("hints pushed once each way", sorted(hub.sent),
          [(1, "192.168.0.2"), (2, "192.168.0.5")])

    # drills: pretend the SMS box / the plug is gone for a while
    for _ in range(25):
        await step()
    check("before drills: normal", hub.devs[1].health_case, "1")
    check("bad drill refused", h.drill(1, "fire", 60)["ok"], False)
    h.drill(1, "sms_off", 120)
    await step()
    check("drill sms_off -> case 2", hub.devs[1].health_case, "2")
    await step(120)
    check("drill ends by itself", hub.devs[1].health_case, "1")
    n = len(msgs)
    h.drill(1, "plug_missing", 600)
    await step()
    check("drill plug_missing -> case 4, hold", (hub.devs[1].health_case,
                                                 hub.devs[1].power_hold), ("4", True))
    check("snapshot lists it", list(h.snapshot(1)["drills"]), ["plug_missing"])
    h.drill(1, "clear", 0)
    await step()
    check("clear -> normal", hub.devs[1].health_case, "1")

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
