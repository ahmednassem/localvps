"""Offline test of cluster_local.LeadFallback with a fake the VPS, peer and plugs."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import cluster_local as cl

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


class Clock:
    t = 100_000.0

    def __call__(self):
        return self.t


class Link:
    def __init__(self):
        self.connected = True
        self.peer_vps = True
        self.sent = []
        self.handlers = {}

    def on(self, kind, fn):
        self.handlers[kind] = fn

    def status(self):
        return {"connected": self.connected,
                "peer_state": {"vps_link": self.peer_vps} if self.connected else {}}

    async def request(self, kind, body=None, timeout=10.0):
        self.sent.append((kind, body))
        return {"ok": True}


class World:
    def __init__(self):
        self.vps = True
        self.battery = False
        self.plugs = {1: {"socket": True, "watts": 20.0}, 2: {"socket": True, "watts": 20.0}}
        self.ops = []
        self.msgs = []
        self.shutdowns = 0

    async def plug(self, op, n):
        self.ops.append((op, n))
        if op == "status":
            return {"ok": True, **self.plugs[n]}
        if op in ("on", "off"):
            self.plugs[n]["socket"] = op == "on"
        return {"ok": True}

    async def notify(self, text):
        self.msgs.append(text)

    async def shutdown(self):
        self.shutdowns += 1
        return {"ok": True}


def make(device=1, shift=1):
    clock, link, w = Clock(), Link(), World()
    tmp = Path(tempfile.mkdtemp())
    lf = cl.LeadFallback(device, link, vps_ok=lambda: w.vps, notify=w.notify,
                         plug=w.plug, on_battery=lambda: w.battery,
                         shutdown=w.shutdown, clock=clock, shift=lambda: shift,
                         log_path=tmp / "log.jsonl", sent_path=tmp / "sent.jsonl")
    return lf, clock, link, w


async def run(lf, clock, secs, step=30):
    for _ in range(int(secs // step)):
        clock.t += step
        await lf.tick()


async def main():
    check("shift device 01:00 -> 2", cl.shift_device(cl.datetime(2026, 9, 28, 1, tzinfo=cl.TZ)), 2)
    check("shift device 07:00 -> 1", cl.shift_device(cl.datetime(2026, 9, 28, 7, tzinfo=cl.TZ)), 1)
    check("shift device 13:00 -> 2", cl.shift_device(cl.datetime(2026, 9, 28, 13, tzinfo=cl.TZ)), 2)
    check("shift device 19:00 -> 1", cl.shift_device(cl.datetime(2026, 9, 28, 19, tzinfo=cl.TZ)), 1)

    lf, clock, link, w = make()
    await run(lf, clock, 3600)
    check("the VPS fine: never active", (lf.active, w.ops), (False, []))

    w.vps = False
    await run(lf, clock, 3600)
    check("peer still has the VPS: no takeover", lf.active, False)

    link.peer_vps = False
    await run(lf, clock, 240)
    check("under 5 min: not yet", lf.active, False)
    await run(lf, clock, 90)
    check("after 5 min: lead takes over", lf.active, True)
    await run(lf, clock, 1200)
    check("idle under 30 min: no shutdown", link.sent, [])
    await run(lf, clock, 360)
    check("idle 30 min: peer told to shut down", [k for k, _ in link.sent], ["shutdown"])

    link.connected = False
    w.plugs[2]["watts"] = 1.0
    await run(lf, clock, 30)
    check("down but inside the settle: no cut", ("off", 2) in w.ops, False)
    await run(lf, clock, 60)
    check("down 60 s, no draw: peer plug cut", ("off", 2) in w.ops, True)
    check("cut once", w.ops.count(("off", 2)), 1)
    await run(lf, clock, 300)
    check("still once", w.ops.count(("off", 2)), 1)

    w.battery = True
    w.plugs[1]["socket"] = False
    await run(lf, clock, 30)
    check("own plug on at self-boot", ("on", 1) in w.ops, True)

    w.vps = True
    await run(lf, clock, 30)
    check("the VPS back: hand back", lf.active, False)
    check("replayed once", len(w.msgs), 1)
    check("replay lists actions", all(a in w.msgs[0] for a in
                                      ("takeover", "peer_shutdown", "peer_plug_off",
                                       "own_plug_on")), True)
    check("log moved to sent", (lf._log_path.exists(), lf._sent_path.exists()), (False, True))

    lf, clock, link, w = make(device=1, shift=2)
    w.vps, link.peer_vps = False, False
    await run(lf, clock, 3600)
    check("not the shift device: no takeover", (lf.active, link.sent), (False, []))
    link.connected = False
    await run(lf, clock, 30)
    check("peer gone: this device leads", lf.active, True)

    lf, clock, link, w = make(device=2)
    res = await link.handlers["shutdown"]({"from": 1})
    check("order refused while own the VPS link is up", res.get("ok"), False)
    w.vps = False
    res = await link.handlers["shutdown"]({"from": 1, "reason": "test"})
    await asyncio.sleep(2.2)
    check("order obeyed without the VPS", (res.get("ok"), w.shutdowns), (True, 1))

    lf, clock, link, w = make()
    w.vps, link.peer_vps = False, False
    await run(lf, clock, 2100)
    check("second run: shutdown sent", len(link.sent), 1)
    await run(lf, clock, 960)
    check("peer never went down: cut skipped", ("off", 2) in w.ops, False)

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
