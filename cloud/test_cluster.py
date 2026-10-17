"""Table test of cluster.node_state plus one Cluster tick with fakes."""
from __future__ import annotations

import sys

import cluster
from cluster import NodeInputs, node_state

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


def st(**kw):
    return node_state(NodeInputs(**kw))


check("control link -> online", st(control=True), "online")
check("fresh beat only -> online", st(seen=True), "online")
check("peer sees it only -> online", st(peer_seen=True), "online")
check("waking, silent -> booting", st(waking=True, socket=True, watts=20), "booting")
check("waking but running -> online", st(waking=True, control=True), "online")
check("plug off -> off", st(socket=False), "off")
check("plug on, no draw -> off", st(socket=True, watts=0.4), "off")
check("plug on, drawing -> unreachable", st(socket=True, watts=20), "unreachable")
check("no plug reading -> unreachable", st(), "unreachable")
check("switched off, still running -> shutting_down",
      st(intentional_off=True, control=True), "shutting_down")
check("switched off, down, cut pending -> shutting_down",
      st(intentional_off=True, cut_pending=True, socket=True, watts=12), "shutting_down")
check("switched off, down, cut done -> off",
      st(intentional_off=True, socket=False), "off")
check("switched off, only peer sees it -> shutting_down",
      st(intentional_off=True, peer_seen=True), "shutting_down")


class Dev:
    def __init__(self, i):
        self.id = i
        self.control = None
        self.intentional_offline = False
        self.suppress_deadman_until = 0.0
        self.node_state = None
        self.node_since = 0.0
        self.peer_seen = False
        self.beat = True
        self.version = "aaa"

    def online(self):
        return self.beat

    def telemetry(self):
        return {"version": self.version}

    @property
    def running(self):
        return self.control is not None or self.peer_seen


class Hub:
    def __init__(self):
        self.devs = {1: Dev(1), 2: Dev(2)}

    def device(self, d):
        return self.devs[d]


class Health:
    def __init__(self):
        self.sees = {1: False, 2: False}
        self.readings = {1: {"socket": True, "watts": 12}, 2: {"socket": True, "watts": 12}}

    def plug_reading(self, d):
        return self.readings[d]

    def peer_sees(self, d):
        return self.sees[d]


class Svc:
    class vps:
        cut_pending = False

    class plug:
        min_watts = 5.0


hub, health = Hub(), Health()
c = cluster.Cluster(hub, {1: Svc(), 2: Svc()}, health)
check("tick: both online", c.tick(), {1: "online", 2: "online"})
hub.devs[2].beat = False
health.sees[2] = True
check("device 2 silent at the VPS, peer sees it", c.tick()[2], "online")
check("peer_seen published", (hub.devs[2].peer_seen, hub.devs[2].running), (True, True))
health.sees[2] = False
check("then nobody sees it, plug drawing", c.tick()[2], "unreachable")
check("running now False", hub.devs[2].running, False)
check("events kept", [(e["device"], e["from"], e["to"]) for e in c.events],
      [(1, None, "online"), (2, None, "online"), (2, "online", "unreachable")])
check("snapshot", c.snapshot()["nodes"][2]["state"], "unreachable")

# agent versions: alert once after the grace, and again when they match
import asyncio  # noqa: E402

sent = []


async def note(m):
    sent.append(m)


async def versions():
    hub2 = Hub()
    v = cluster.Cluster(hub2, {1: Svc(), 2: Svc()}, Health(), notify=note,
                        version_grace_s=600)
    t = [1000.0]
    cluster.time.time = lambda: t[0]
    v.tick()
    hub2.devs[2].version = "bbb"
    v.tick()
    t[0] += 300
    v.tick()
    await asyncio.sleep(0)
    check("differ < grace: no alert", sent, [])
    t[0] += 301
    v.tick()
    v.tick()
    await asyncio.sleep(0)
    check("differ > grace: one alert", [m[:24] for m in sent], ["⚠ Agent versions differ:"])
    check("snapshot shows mismatch", v.snapshot()["versions_match"], False)
    hub2.devs[2].version = "aaa"
    v.tick()
    await asyncio.sleep(0)
    check("match again noted", sent[-1].startswith("✅"), True)
    hub2.devs[1].beat = False
    hub2.devs[1].version = "zzz"
    v.tick()
    check("offline device not compared", v.snapshot()["versions"], {2: "aaa"})

asyncio.run(versions())

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
