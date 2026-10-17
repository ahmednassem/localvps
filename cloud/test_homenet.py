"""Offline checks for homenet.py. Run: python test_homenet.py"""
import asyncio
import sys

import homenet
from homenet import NetWatch, assess, plug_line

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


def lap(label="laptop1", **kw):
    base = dict(label=label, seen=True, net="wifi", internet=True, router_ok=True,
                wifi="on", sms="on", sees_plugs=[])
    return {**base, **kw}


def plug(n=1, **kw):
    return {"n": n, "label": f"plug {n}", "online": True, "wifi": "main", **kw}


def states(laptops, plugs):
    v = assess(laptops, plugs)
    return v["router"], v["sms"], v["home"]


# ── router / SMS box / home cases
check("all normal", states([lap(), lap("laptop2", net="eth")], [plug(1), plug(2)]),
      ("ok", "ok", "ok"))
check("router down: plug on SMS Wi-Fi, laptops on SMS",
      states([lap(net="sms", router_ok=False, wifi="off")], [plug(1, wifi="backup")]),
      ("down", "ok", "ok"))
check("router down seen by laptops only",
      states([lap(net="sms", router_ok=False, wifi="off")], [plug(1, online=False)]),
      ("down", "ok", "ok"))
check("router down, plug still on SMS Wi-Fi after router is back -> ok wins",
      states([lap()], [plug(1, wifi="backup")]), ("ok", "ok", "ok"))
check("router up, no internet: laptop reaches it, no internet",
      states([lap(internet=False)], []), ("no_internet", "ok", "ok"))
check("router up, no internet: laptop sees plug on LAN, plug off the VPS",
      states([lap(net="sms", sees_plugs=[1])], [plug(1, online=False)]),
      ("no_internet", "ok", "ok"))
check("laptop on SMS by rotation, router fine -> not a fault",
      states([lap(net="sms")], [plug(1, online=False)]), ("unknown", "ok", "ok"))
check("SMS box down", states([lap(sms="off")], [plug(1)]), ("ok", "down", "ok"))
check("SMS box down, one laptop still sees it -> ok",
      states([lap(sms="off"), lap("laptop2", sms="on")], []), ("ok", "ok", "ok"))
check("SMS box absent on a laptop says nothing",
      states([lap(sms="absent")], []), ("ok", "unknown", "ok"))
check("router down and SMS box down, plugs silent, laptops silent",
      states([lap(seen=False), lap("laptop2", seen=False)],
             [plug(1, online=False), plug(2, online=False)]),
      ("unknown", "unknown", "unreachable"))
check("laptops off, plugs on main Wi-Fi -> router ok",
      states([lap(seen=False)], [plug(1)]), ("ok", "unknown", "ok"))
check("laptops off, plug on SMS Wi-Fi -> router down, SMS ok",
      states([lap(seen=False)], [plug(1, wifi="backup")]), ("down", "ok", "ok"))
check("unseen laptop's data ignored",
      states([lap(seen=False, sms="off", wifi="off", router_ok=False)], [plug(1)]),
      ("ok", "unknown", "ok"))

# ── plug status line
check("line online main", plug_line("plug 1", {"ok": True, "online": True, "socket": True,
                                               "watts": 12.0, "volts": 230.1,
                                               "wifi": "main", "signal": -55}),
      "🟢 plug 1: socket ON · 12.0 W · 230.1 V · main Wi-Fi -55 dBm")
check("line on backup", plug_line("plug 2", {"ok": True, "online": True, "socket": False,
                                             "wifi": "backup"})[:1], "🟠")
check("line offline", plug_line("plug 2", {"ok": True, "online": False, "seen_s": 600}),
      "🔴 plug 2: OFFLINE — not connected to the VPS, last seen 10 min ago")
check("line no data", plug_line("plug 2", None).startswith("🔌 plug 2: no data"), True)


# ── alerts
class Clock:
    t = 1000.0


clock = Clock()
homenet.time.time = lambda: clock.t


async def main():
    msgs = []

    async def note(m):
        msgs.append(m)

    w = NetWatch(note)
    laptops = [lap(), lap("laptop2", net="eth")]
    plugs = [plug(1, known=True), plug(2, known=True)]

    async def run(ls, ps, seconds=30, announce=True):
        clock.t += seconds
        await w.update(assess(ls, ps), ps, announce=announce)

    await run(laptops, plugs, announce=False)
    check("first view: no messages", msgs, [])
    check("status line", w.status_line(), "🌐 Home: main router OK · SMS box OK")

    down_l = [lap(net="sms", router_ok=False, wifi="off"),
              lap("laptop2", net="sms", router_ok=False, wifi="off")]
    down_p = [plug(1, known=True, wifi="backup"), plug(2, known=True, wifi="backup")]
    await run(down_l, down_p)
    check("router not announced before 90 s", any("router is DOWN" in m for m in msgs), False)
    check("plug Wi-Fi change not announced before 90 s", any("📶" in m for m in msgs), False)
    for _ in range(3):
        await run(down_l, down_p)
    check("plugs moved to SMS Wi-Fi: two messages",
          sum(m.startswith("📶") for m in msgs), 2)
    check("router down announced once", sum("Main router is DOWN" in m for m in msgs), 1)
    check("status line down", w.status_line(), "🌐 Home: main router DOWN · SMS box OK")

    await run(laptops, plugs)
    check("short blip back: not yet", any("Main router back" in m for m in msgs), False)
    await run(down_l, down_p)
    await run(laptops, plugs)
    for _ in range(4):
        await run(laptops, plugs)
    check("router back announced once", sum("Main router back" in m for m in msgs), 1)
    check("plugs back on main Wi-Fi",
          sum("back on the main router Wi-Fi" in m for m in msgs), 2)

    msgs.clear()
    sms_down = [lap(sms="off"), lap("laptop2", net="eth", sms="off")]
    for _ in range(5):
        await run(sms_down, plugs)
    check("SMS box down", [m for m in msgs if "SMS box" in m],
          ["⚠️ SMS box is DOWN (laptop1 can't see it; laptop2 can't see it) — no backup internet."])
    for _ in range(5):
        await run(laptops, plugs)
    check("SMS box back", any(m.startswith("✅ SMS box back") for m in msgs), True)

    msgs.clear()
    off = [plug(1, known=True, online=False, lan_by="laptop1"),
           plug(2, known=True, online=False)]
    for _ in range(3):
        await run(laptops, off)
    check("plug offline: not before 2 min", any("OFFLINE" in m for m in msgs), False)
    for _ in range(3):
        await run(laptops, off)
    check("plug 1 offline, laptop reaches it",
          any("plug 1 is OFFLINE" in m and "laptop1 still reaches it" in m for m in msgs), True)
    check("plug 2 offline, nobody reaches it",
          any("plug 2 is OFFLINE" in m and "can't be switched" in m for m in msgs), True)
    check("plug offline sent once each", sum("OFFLINE" in m for m in msgs), 2)
    await run(laptops, [plug(1, known=True, view={"ok": True, "online": True,
                                                   "socket": True, "wifi": "main"}),
                        plug(2, known=True)])
    check("plug back online", sum("back online" in m for m in msgs), 2)

    msgs.clear()
    gone = [lap(seen=False), lap("laptop2", seen=False)]
    for _ in range(5):
        await run(gone, off)
    check("home unreachable", any(m.startswith("🏠 Nothing at home") for m in msgs), True)
    for _ in range(5):
        await run(laptops, plugs)
    check("home back", any(m == "✅ Home reachable again." for m in msgs), True)

    msgs.clear()
    noinet = [lap(internet=False), lap("laptop2", net="eth", internet=False)]
    for _ in range(5):
        await run(noinet, off)
    check("router no internet", any("NO internet" in m for m in msgs), True)


asyncio.run(main())
print("\nall ok" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
