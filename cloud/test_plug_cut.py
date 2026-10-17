"""Offline checks for the plug cut after a shutdown (fake hub, device, plug).

Run: python test_plug_cut.py
"""
from __future__ import annotations

import asyncio
import sys

import vps_power
from vps_power import VpsPower

fails = 0


def check(name: str, got, want) -> None:
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (want {want!r})"))


class FakeDev:
    def __init__(self) -> None:
        self.id = 2
        self.label = "laptop2"
        self.intentional_offline = False
        self.control = object()
        self.power_hold = False
        self.peer_seen = False
        self.suppress_deadman_until = 0.0

    def persist_offline_flag(self) -> None:
        pass


class FakeHub:
    def __init__(self, dev: FakeDev) -> None:
        self.dev = dev

    def device(self, _id: int) -> FakeDev:
        return self.dev

    async def send_command(self, *_a, **_k) -> dict:
        return {"ok": True}


class FakePlug:
    configured = True
    min_watts = 5.0

    def __init__(self, watts: float = 1.0, off_ok: bool = True) -> None:
        self.watts = watts
        self.off_ok = off_ok
        self.offs = 0
        self.cycles = 0

    async def status(self) -> dict:
        return {"ok": True, "online": True, "socket": True, "watts": self.watts}

    async def off(self) -> dict:
        self.offs += 1
        return {"ok": True} if self.off_ok else {"ok": False, "error": "plug offline"}

    async def cycle(self) -> dict:
        self.cycles += 1
        return {"ok": True}


class Clock:
    """Fake time: every asyncio.sleep in vps_power advances it."""

    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


clock = Clock()
_real_sleep = asyncio.sleep


async def _fast_sleep(s: float) -> None:
    clock.t += s
    await _real_sleep(0)


vps_power.time = clock                       # type: ignore[assignment]
vps_power.asyncio.sleep = _fast_sleep        # type: ignore[assignment]


def make(watts: float = 1.0, off_ok: bool = True):
    dev = FakeDev()
    plug = FakePlug(watts, off_ok)
    msgs: list[str] = []

    async def notify(m: str) -> None:
        msgs.append(m)

    vps = VpsPower(FakeHub(dev), 2, plug, notify, cut_settle_s=60,
                   cut_max_wait_s=300, cut_give_up_s=900, cut_poll_s=10)
    return dev, plug, vps, msgs


async def run_until(vps: VpsPower, step, limit: int = 200) -> None:
    """Drive the cut task; `step(i)` can change the fake world each tick."""
    for i in range(limit):
        if vps._cut_task is None or vps._cut_task.done():
            return
        step(i)
        await _real_sleep(0)


async def main() -> None:
    # 1. link drops, low draw -> cut after the settle time, not before
    dev, plug, vps, msgs = make(watts=1.0)
    t0 = clock.t
    await vps.offline()
    await run_until(vps, lambda i: setattr(dev, "control", None) if i == 2 else None)
    check("low draw: plug cut", plug.offs, 1)
    check("cut waited the settle time", clock.t - t0 >= 60, True)
    check("notice sent", any("plug switched off" in m for m in msgs), True)

    # 1b. control link gone but the other device still sees it -> wait
    dev, plug, vps, msgs = make(watts=1.0)
    await vps.offline()
    dev.control = None
    dev.peer_seen = True
    t0 = clock.t

    def peer_gone(i: int) -> None:
        if clock.t - t0 >= 200:
            dev.peer_seen = False
    await run_until(vps, peer_gone)
    check("peer still sees it: cut waits", clock.t - t0 >= 200, True)
    check("then cut once the peer loses it", plug.offs, 1)

    # 2. a charging laptop keeps drawing -> cut at the max wait
    dev, plug, vps, msgs = make(watts=30.0)
    t0 = clock.t
    await vps.offline()
    dev.control = None
    await run_until(vps, lambda i: None)
    check("high draw: plug cut at max wait", plug.offs, 1)
    check("waited for the max wait", clock.t - t0 >= 300, True)

    # 3. a wake during the wait -> no cut
    dev, plug, vps, msgs = make(watts=30.0)
    await vps.offline()
    dev.control = None

    def wake(i: int) -> None:
        if i == 3:
            dev.intentional_offline = False
    await run_until(vps, wake)
    check("woken during the wait: no cut", plug.offs, 0)

    # 4. /online cancels the cut task and cycles the plug
    dev, plug, vps, msgs = make(watts=30.0)
    await vps.offline()
    dev.control = None
    await _real_sleep(0)
    await vps.online(notify=False)
    await _real_sleep(0)
    check("online cancels the cut", (plug.offs, plug.cycles, vps._cut_task.done()), (0, 1, True))

    # 5. the agent reconnects after going down -> no cut
    dev, plug, vps, msgs = make(watts=30.0)
    await vps.offline()
    dev.control = None

    def back(i: int) -> None:
        if i == 3:
            dev.control = object()
    await run_until(vps, back)
    check("agent came back: no cut", plug.offs, 0)

    # 6. never goes down -> give up, plug left on, warning sent
    dev, plug, vps, msgs = make(watts=30.0)
    await vps.offline()
    await run_until(vps, lambda i: None)
    check("never went down: no cut", plug.offs, 0)
    check("never went down: warning", any("still connected" in m for m in msgs), True)

    # 7. plug refuses -> warning
    dev, plug, vps, msgs = make(watts=1.0, off_ok=False)
    await vps.offline()
    dev.control = None
    await run_until(vps, lambda i: None)
    check("plug error: tried once", plug.offs, 1)
    check("plug error: warning", any("could not be switched off" in m for m in msgs), True)

    # 8. switched off by env -> no cut task at all
    dev, plug, vps, msgs = make()
    vps._cut_after_shutdown = False
    await vps.offline()
    check("disabled: no cut task", vps._cut_task, None)

    # 9. the device comes up by itself with the plug off -> plug on
    from alerts import Watchdog

    class StatusPlug(FakePlug):
        def __init__(self, socket: bool, online: bool = True) -> None:
            super().__init__()
            self.socket, self.plug_online, self.ons = socket, online, 0

        async def status(self) -> dict:
            return {"ok": True, "online": self.plug_online, "socket": self.socket, "watts": 0.0}

        async def on(self) -> dict:
            self.ons += 1
            self.socket = True
            return {"ok": True}

    for name, plug, want in (("self boot, plug off -> on", StatusPlug(False), 1),
                             ("plug already on -> untouched", StatusPlug(True), 0),
                             ("plug unreachable -> untouched", StatusPlug(False, online=False), 0)):
        msgs = []

        async def note(m: str) -> None:
            msgs.append(m)
        wd = Watchdog(FakeHub(FakeDev()), 2, plug, note)
        await wd._power_plug_if_off()
        check(name, plug.ons, want)

    print("ALL PASSED" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
