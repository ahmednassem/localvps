"""Offline test of the login uplink rotation: two paths per device, SMS
fallback to the router, SMS marked bad, box reboots every 4 switches."""
from __future__ import annotations

import asyncio
import sys

import netrotate
from netrotate import BoxReboots, NetRotate

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


class Clock:
    t = 50_000.0


clock = Clock()
netrotate.time.time = lambda: clock.t


_real_sleep = asyncio.sleep


async def _no_sleep(s):
    clock.t += s
    await _real_sleep(0)
asyncio.sleep = _no_sleep


class Dev:
    def __init__(self, i, net):
        self.id, self.label, self.net = i, f"laptop{i}", net

    def status_snapshot(self):
        return {"net": self.net}

    def online(self):
        return True


class Hub:
    def __init__(self):
        self.devs = {1: Dev(1, "wifi"), 2: Dev(2, "eth")}
        self.sms_internet = True
        self.sms_up = True
        self.calls = []

    def device(self, d):
        return self.devs[d]

    async def send_command(self, d, kind, name, args=None, timeout=30.0):
        self.calls.append((d, name, dict(args or {})))
        dev = self.devs[d]
        if name == "net":
            src = args["source"]
            if src == "sms" and not self.sms_up:
                return {"ok": False, "error": "sms box not found"}
            dev.net = src
            return {"ok": True, "ip": "1.2.3.4"}
        if name == "router" and args.get("op") == "ip":
            ok = dev.net != "sms" or self.sms_internet
            return {"ok": ok, "public_ip": "5.6.7.8" if ok else None}
        if name == "router" and args.get("op") == "reboot":
            return {"ok": True, "came_back": True, "active": dev.net}
        return {"ok": True}


msgs = []


async def note(m):
    msgs.append(m)


def make(hub, reboot=None):
    br = BoxReboots(note, every=4, reboot=reboot or {}, main=lambda: 1)
    boxes = {"wifi": "dsl", "eth": "dsl", "sms": "sms"}
    n1 = NetRotate(hub, 1, note, every_tasks=20, cooldown_s=0, paths=("wifi", "sms"),
                   boxes=boxes, reboots=br, sms_check_s=30, sms_bad_s=600)
    n2 = NetRotate(hub, 2, note, every_tasks=20, cooldown_s=0, paths=("eth", "sms"),
                   boxes=boxes, reboots=br, sms_check_s=30, sms_bad_s=600)
    return br, n1, n2


async def logins(nr, n, outcome="done"):
    for _ in range(n):
        await nr.report({"event": "login_attempt", "outcome": outcome, "inflight": 0})
        await asyncio.sleep(0)
    for _ in range(100):
        await _real_sleep(0)


async def main():
    hub = Hub()
    br, n1, n2 = make(hub)
    await logins(n1, 19)
    check("19 logins: still on wifi", hub.devs[1].net, "wifi")
    await logins(n1, 1)
    check("20 logins: device 1 -> sms", hub.devs[1].net, "sms")
    check("switch counted", br.switches, 1)
    await logins(n1, 20)
    check("20 more: back to its router path (wifi)", hub.devs[1].net, "wifi")
    await logins(n2, 20)
    check("device 2 counts its own: eth -> sms", hub.devs[2].net, "sms")
    check("device 1 untouched", hub.devs[1].net, "wifi")

    hub.sms_internet = False
    clock.t += 10
    await logins(n1, 20)
    check("sms without internet -> straight back to wifi", hub.devs[1].net, "wifi")
    check("sms marked bad", n1.sms_bad(), True)
    check("fallback still counts as a switch", br.switches, 4)
    check("alert sent", any("no internet" in m for m in msgs), True)
    await logins(n1, 20)
    check("while sms bad: stays on wifi", hub.devs[1].net, "wifi")
    clock.t += 601
    hub.sms_internet = True
    await logins(n1, 20)
    check("after 10 min: sms again", hub.devs[1].net, "sms")

    hub.sms_up = False
    hub.devs[2].net = "eth"
    n2._current = "eth"
    await logins(n2, 20)
    check("sms box not found -> stays on eth", hub.devs[2].net, "eth")
    check("and marked bad", n2.sms_bad(), True)

    check("snapshot has switches/next reboot",
          (n1.snapshot()["switches"], n1.snapshot()["box_reboot_in"]), (br.switches, br.reboot_in))

    # box reboots every 4 switches, main device does them, devices moved off first
    hub = Hub()
    msgs.clear()
    br, n1, n2 = make(hub, reboot={"sms": True, "dsl": True})
    hub.devs[2].net = "sms"
    n2._current = "sms"
    res = await br.run()
    check("both boxes rebooted", (res["sms"], res["dsl"]), ("ok", "ok"))
    reboots = [(d, a["target"]) for d, n, a in hub.calls if n == "router" and a.get("op") == "reboot"]
    check("main device (1) rebooted sms then dsl", reboots, [(1, "sms"), (1, "dsl")])
    order = [(d, n, a.get("source") or a.get("target") or a.get("op")) for d, n, a in hub.calls]
    i_sms = order.index((1, "router", "sms"))
    check("device 2 left sms before the sms reboot",
          (2, "net", "eth") in order[:i_sms], True)
    i_dsl = order.index((1, "router", "dsl"))
    check("both on sms before the router reboot",
          {(1, "net", "sms"), (2, "net", "sms")} <= set(order[i_sms:i_dsl]), True)

    hub = Hub()
    br, n1, n2 = make(hub, reboot={"sms": True, "dsl": True})
    hub.sms_internet = False
    res = await br.run()
    check("router reboot skipped when nobody can leave it", res["dsl"], "skipped")

    hub = Hub()
    br, n1, n2 = make(hub)
    for _ in range(4):
        br.note_switch()
    await asyncio.sleep(0)
    check("reboots off: nothing rebooted",
          [c for c in hub.calls if c[1] == "router" and c[2].get("op") == "reboot"], [])

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
