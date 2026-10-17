"""netrotate.py — spread a device's work across its three uplinks.

Same shape as night.py: policy lives here on the gate, the device just obeys.
One NetRotate instance per device. The lsvc task gate forwards every
web_login attempt the worker reports:

  POST /api/net/report  {"device":1, "event":"login_attempt",
                         "outcome":"done|rejected|auth_error|failed",
                         "inflight":N}
  POST /api/net/report  {"device":1, "event":"idle", "inflight":0}

and this module decides when to move the route between the three uplinks
(each is its own box with its own WAN → own public IP):

  wifi  main DSL router (Wi‑Fi)            IP changes on router reboot
  eth   second router (Ethernet cable)        IP changes on router reboot
  sms   SMS box (hotspot / USB)       mobile IP, changes on its own

Rules (all tunable through NETROTATE_* in .env):
  * a stint on one path is EVERY_TASKS login attempts (20), then rotate to the
    next path in the cycle wifi → eth → sms → wifi;
  * rotate early after REJECT_STREAK (3) rejected attempts in a row on this path;
  * never land a rotation while the worker has anything in flight — wait for
    inflight == 0 (the worker sends an idle frame), but force after MAX_DEFER_S;
  * COOLDOWN_S between rotations, whatever the counters say;
  * when a path is dead/unreachable, skip it in the cycle with a DEAD_PENALTY
    (50) attempt sentence before trying it again;
  * every REPORT_EVERY (100) attempts on a path, send a Telegram summary;
  * when we leave a router path, optionally reboot its box in the background
    (router_mgr's interlock: we are already on a survivor) so it comes back
    with a fresh IP for the next stint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from hub import Hub

log = logging.getLogger("netrotate")

Notify = Callable[[str], Awaitable[None]]

PATHS = ("wifi", "eth", "sms")
LABEL = {"wifi": "main Wi‑Fi (DSL)", "eth": "Ethernet router", "sms": "SMS box"}
PATH_BOX = {"wifi": "dsl", "eth": "eth", "sms": "sms"}   # path -> box to reboot


def _cycle_after(path: str, paths: tuple[str, ...] = PATHS) -> list[str]:
    """Other paths in rotation order, starting after `path`."""
    if path not in paths:
        path = paths[0]
    i = paths.index(path)
    return [paths[(i + k) % len(paths)] for k in range(1, len(paths))]


def shift_device() -> int:
    """The main device now (same slots as the lsvc router, whose
    LOGIN_PRIMARY must equal SHIFT_FIRST): SHIFT_FIRST in 00-06 and 12-18."""
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo
    first = int(os.environ.get("SHIFT_FIRST", "2") or 2)
    h = datetime.now(ZoneInfo("UTC")).hour
    return first if (0 <= h < 6 or 12 <= h < 18) else (1 if first == 2 else 2)


class BoxReboots:
    """Shared by every device's NetRotate: the boxes are shared, so the switch
    count and the reboots are cluster-wide. Every `every` switches the SMS box
    and then the router are rebooted, each one only after every device on it
    was moved to the other box and confirmed online. The main device carries
    out the reboots. Boxes whose reboot flag is off are skipped."""

    def __init__(self, notify: Notify, *, every: int = 4,
                 reboot: dict[str, bool] | None = None,
                 main: Callable[[], int] = shift_device) -> None:
        self._notify = notify
        self.every = max(1, every)
        self.reboot = reboot or {}           # box -> allowed
        self._main = main
        self.rotators: dict[int, "NetRotate"] = {}
        self.switches = 0
        self.busy = False
        self.last: dict | None = None

    def add(self, device_id: int, nr: "NetRotate") -> None:
        self.rotators[device_id] = nr

    @property
    def reboot_in(self) -> int:
        return self.every - (self.switches % self.every)

    def note_switch(self) -> None:
        self.switches += 1
        if self.switches % self.every == 0 and not self.busy:
            if any(self.reboot.get(b) for b in ("sms", "dsl")):
                asyncio.create_task(self.run())
            else:
                log.info("%d switches: box reboots are off", self.switches)

    async def run(self) -> dict:
        self.busy = True
        done: dict[str, str] = {}
        try:
            for box in ("sms", "dsl"):
                if not self.reboot.get(box):
                    done[box] = "off"
                    continue
                done[box] = await self._reboot(box)
        finally:
            self.busy = False
        self.last = {"at": int(time.time()), "switches": self.switches, **done}
        return self.last

    async def _reboot(self, box: str) -> str:
        movers = [nr for nr in self.rotators.values() if nr.online()]
        for nr in movers:
            if nr.box_of(nr.current()) != box:
                continue
            away = nr.path_off(box)
            if away is None or not await nr.move_to(away, f"{box} reboot"):
                await self._notify(f"⚠ {box.upper()} reboot skipped: "
                                   f"{nr.label} could not leave it")
                return "skipped"
        main = self.rotators.get(self._main())
        if main is None or not main.online():
            main = next(iter(movers), None)
        if main is None:
            return "no device"
        res = await main.reboot_box(box, why=f"every {self.every} switches")
        return "ok" if res.get("ok") else f"failed: {res.get('error', '?')}"


class PathStats:
    __slots__ = ("attempts", "done", "rejected", "auth_error", "failed",
                 "since", "report_mark", "stint_attempts", "stint_limit",
                 "streak")

    def __init__(self) -> None:
        self.attempts = 0
        self.done = 0
        self.rejected = 0
        self.auth_error = 0
        self.failed = 0
        self.since = time.time()
        self.report_mark = 0          # attempts at last periodic report
        self.stint_attempts = 0       # attempts since we landed on this path
        self.stint_limit = 0          # attempts allowed this stint
        self.streak = 0               # consecutive rejected

    def as_dict(self) -> dict:
        return {
            "attempts": self.attempts, "done": self.done,
            "rejected": self.rejected, "auth_error": self.auth_error,
            "failed": self.failed,
            "stint": f"{self.stint_attempts}/{self.stint_limit}",
            "reject_streak": self.streak,
            "since": int(self.since),
        }


class NetRotate:
    def __init__(self, hub: Hub, device_id: int, notify: Notify, *,
                 enabled: bool = True,
                 every_tasks: int = 20,
                 reject_streak: int = 3,
                 report_every: int = 100,
                 dead_penalty_tasks: int = 50,
                 cooldown_s: float = 300.0,
                 max_defer_s: float = 300.0,
                 reboot_dsl_on_leave: bool = True,
                 reboot_eth_on_leave: bool = False,
                 reboot_sms_on_leave: bool = False,
                 poll: float = 15.0,
                 paths: tuple[str, ...] = PATHS,
                 boxes: dict[str, str] | None = None,
                 sms_check_s: float = 45.0,
                 sms_bad_s: float = 600.0,
                 reboots: BoxReboots | None = None) -> None:
        self._paths = tuple(p for p in paths if p in PATHS) or PATHS
        self._boxes = boxes or PATH_BOX
        self._sms_check_s = sms_check_s
        self._sms_bad_s = sms_bad_s
        self._sms_bad_until = 0.0
        self._reboots = reboots
        if reboots is not None:
            reboots.add(device_id, self)
        self._hub = hub
        self._dev = hub.device(device_id)
        self._notify = notify
        self.enabled = enabled
        self._every = max(1, every_tasks)
        self._reject_streak = max(1, reject_streak)
        self._report_every = max(1, report_every)
        self._dead_penalty = max(1, dead_penalty_tasks)
        self._cooldown = cooldown_s
        self._max_defer = max_defer_s
        self._reboot_on_leave = {
            "wifi": reboot_dsl_on_leave,
            "eth": reboot_eth_on_leave,
            "sms": reboot_sms_on_leave,
        }
        self._poll = poll

        self._stats: dict[str, PathStats] = {p: PathStats() for p in PATHS}
        self._current = ""              # wifi|eth|sms as far as we know
        self._trust_self_until = 0.0    # ignore stale telemetry until then
        self._inflight = 0
        self._last_report_at = 0.0
        self._last_rotate_at = 0.0
        self._pending_since = 0.0       # >0 while a rotation is waiting for idle
        self._pending_reason = ""
        self._rotating = False
        self._rotations = 0
        self._last_reboot: dict[str, dict] = {}
        self._dead_until_attempts: dict[str, int] = {}   # path -> penalty left
        self._lock = asyncio.Lock()
        for p in PATHS:
            self._stats[p].stint_limit = self._every

    @property
    def _label(self) -> str:
        return self._dev.label

    # ── helpers ────────────────────────────────────────────────────────────

    def _sync_current(self) -> str:
        """Trust the device's own telemetry for which path it is on — except
        right after we moved it ourselves: the heartbeat is only every ~60s,
        so for a grace window our own value is the fresher one."""
        if time.time() < self._trust_self_until:
            return self._current
        net = str(self._dev.status_snapshot().get("net") or "").split(":")[0]
        if net in PATHS:
            if net != self._current:
                if self._current:
                    log.info("[%s] path changed outside of us: %s -> %s",
                             self._label, self._current, net)
                self._current = net
        elif not self._current:
            self._current = self._paths[0]
        return self._current

    # used by BoxReboots
    @property
    def label(self) -> str:
        return self._dev.label

    def current(self) -> str:
        return self._sync_current()

    def online(self) -> bool:
        return self._dev.online()

    def box_of(self, path: str) -> str | None:
        return self._boxes.get(path)

    def path_off(self, box: str) -> str | None:
        """A path of this device that doesn't go through `box`."""
        for p in self._paths:
            if self._boxes.get(p) != box and not (p == "sms" and self.sms_bad()):
                return p
        return None

    def sms_bad(self) -> bool:
        return time.time() < self._sms_bad_until

    def _pick_target(self, cur: str, to: str = "", *,
                     force: bool = False) -> str | None:
        """Next path in the cycle, skipping dead-penalty paths (unless forced
        or explicitly requested) and the SMS box while it is marked bad.
        Falls back to the immediate next path when every other path is under
        penalty; None when the only other path is a bad SMS box."""
        if to in PATHS and to != cur:
            return to
        candidates = _cycle_after(cur, self._paths)
        if self.sms_bad():
            candidates = [p for p in candidates if p != "sms"]
            if not candidates:
                return None
        if not force:
            alive = [p for p in candidates if p not in self._dead_until_attempts]
            if alive:
                return alive[0]
        return candidates[0]

    def snapshot(self) -> dict:
        cur = self._sync_current()
        st = self._stats[cur]
        return {
            "ok": True,
            "device": self._dev.id,
            "enabled": self.enabled,
            "current": cur,
            "inflight": self._inflight,
            "pending_rotation": bool(self._pending_since),
            "pending_reason": self._pending_reason or None,
            "pending_for_s": (int(time.time() - self._pending_since)
                              if self._pending_since else 0),
            "rotating": self._rotating,
            "rotations": self._rotations,
            "last_rotate_ago_s": (int(time.time() - self._last_rotate_at)
                                  if self._last_rotate_at else None),
            "cooldown_left_s": max(0, int(self._cooldown -
                                          (time.time() - self._last_rotate_at)))
            if self._last_rotate_at else 0,
            "stint": f"{st.stint_attempts}/{st.stint_limit}",
            "reject_streak": st.streak,
            "paths": {p: self._stats[p].as_dict() for p in self._paths},
            "dead_penalty": dict(self._dead_until_attempts),
            "last_reboot": self._last_reboot,
            "sms_bad_s": max(0, int(self._sms_bad_until - time.time())),
            "switches": self._reboots.switches if self._reboots else self._rotations,
            "box_reboot_in": self._reboots.reboot_in if self._reboots else None,
            "box_reboots": ({b: on for b, on in self._reboots.reboot.items()}
                            if self._reboots else None),
            "config": {
                "every_tasks": self._every,
                "reject_streak": self._reject_streak,
                "report_every": self._report_every,
                "dead_penalty_tasks": self._dead_penalty,
                "cooldown_s": self._cooldown,
                "max_defer_s": self._max_defer,
                "reboot_dsl_on_leave": self._reboot_on_leave["wifi"],
                "reboot_eth_on_leave": self._reboot_on_leave["eth"],
                "reboot_sms_on_leave": self._reboot_on_leave["sms"],
            },
        }

    def set_enabled(self, on: bool) -> dict:
        self.enabled = bool(on)
        if not on:
            self._pending_since = 0.0
            self._pending_reason = ""
        log.info("[%s] netrotate enabled=%s", self._label, self.enabled)
        return self.snapshot()

    # ── called from the gate API ───────────────────────────────────────────

    async def report(self, frame: dict) -> dict:
        """One frame from the worker (via lsvc). Returns the snapshot."""
        event = str(frame.get("event") or "")
        try:
            self._inflight = max(0, int(frame.get("inflight", self._inflight)))
        except (TypeError, ValueError):
            pass
        self._last_report_at = time.time()
        cur = self._sync_current()

        if event == "login_attempt":
            outcome = str(frame.get("outcome") or "failed")
            st = self._stats[cur]
            st.attempts += 1
            st.stint_attempts += 1
            if outcome == "done":
                st.done += 1
                st.streak = 0
            elif outcome == "rejected":
                st.rejected += 1
                st.streak += 1
            elif outcome == "auth_error":
                st.auth_error += 1
            else:
                st.failed += 1
            # burn down a dead-path penalty while we work here
            for p in list(self._dead_until_attempts):
                self._dead_until_attempts[p] -= 1
                if self._dead_until_attempts[p] <= 0:
                    del self._dead_until_attempts[p]
                    log.info("[%s] dead penalty on %s expired", self._label, p)

            if st.attempts - st.report_mark >= self._report_every:
                st.report_mark = st.attempts
                await self._periodic_report(cur)

            if self.enabled and not self._pending_since and not self._rotating:
                reason = ""
                if st.streak >= self._reject_streak:
                    reason = (f"{st.streak} rejected attempts in a row on "
                              f"{LABEL[cur]}")
                elif st.stint_attempts >= st.stint_limit:
                    reason = (f"{st.stint_attempts} login attempts on "
                              f"{LABEL[cur]} — stint over")
                if reason:
                    self._arm(reason)

        # Any frame with nothing in flight is a chance to land a pending move.
        if self._pending_since and self._inflight == 0:
            asyncio.create_task(self._try_rotate(force=False))
        return self.snapshot()

    def _arm(self, reason: str) -> None:
        if time.time() - self._last_rotate_at < self._cooldown:
            log.info("[%s] rotation wanted (%s) but in cooldown",
                     self._label, reason)
            return
        self._pending_since = time.time()
        self._pending_reason = reason
        log.info("[%s] rotation armed: %s (inflight=%d)",
                 self._label, reason, self._inflight)

    async def rotate_now(self, to: str = "", reason: str = "manual") -> dict:
        """Admin: rotate immediately (still waits for idle unless forced)."""
        self._pending_since = time.time()
        self._pending_reason = reason
        return await self._try_rotate(force=True, to=to)

    # ── the move itself ────────────────────────────────────────────────────

    async def _try_rotate(self, *, force: bool, to: str = "") -> dict:
        async with self._lock:
            if not self._pending_since:
                return self.snapshot()
            if self._reboots is not None and self._reboots.busy:
                return self.snapshot()          # a box reboot is moving devices
            waited = time.time() - self._pending_since
            if self._inflight > 0 and not force and waited < self._max_defer:
                return self.snapshot()          # keep waiting for idle
            if self._inflight > 0:
                log.warning("[%s] rotating with %d task(s) in flight (%s)",
                            self._label, self._inflight,
                            "forced" if force else f"deferred {int(waited)}s")

            cur = self._sync_current()
            target = self._pick_target(cur, to, force=force)
            reason = self._pending_reason
            self._pending_since = 0.0
            self._pending_reason = ""

            if target is None:
                log.info("[%s] stint over but the SMS box is marked bad - "
                         "staying on %s", self._label, cur)
                self._stats[cur].stint_attempts = 0
                self._stats[cur].streak = 0
                return self.snapshot()

            if target in self._dead_until_attempts and not force:
                left = self._dead_until_attempts[target]
                log.info("[%s] skip rotation to %s: dead penalty, "
                         "%d attempts left", self._label, target, left)
                self._stats[cur].stint_attempts = 0
                self._stats[cur].stint_limit = max(left, self._every)
                self._stats[cur].streak = 0
                return self.snapshot()

            self._rotating = True
            try:
                return await self._do_rotate(cur, target, reason)
            finally:
                self._rotating = False

    async def _net(self, target: str) -> dict:
        """Move the device's route. The agent brings the new path up before
        it drops the old one and answers ok only once it's connected."""
        try:
            # remember=False: a rotation is temporary and must not become the
            # device's sticky preference (the user's /net_* choice stays).
            return await self._hub.send_command(
                self._dev.id, "action", "net",
                {"source": target, "remember": False}, timeout=90.0)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "unreachable": True}
        except TimeoutError:
            return {"ok": False, "error": "timed out waiting for the device",
                    "unreachable": True}

    async def _internet_ok(self) -> bool:
        """The device reaches the internet on its current path (public IP
        readable) within sms_check_s."""
        deadline = time.time() + self._sms_check_s
        while True:
            try:
                res = await self._hub.send_command(
                    self._dev.id, "action", "router", {"op": "ip"}, timeout=20.0)
                if res.get("ok") and res.get("public_ip"):
                    return True
            except (RuntimeError, TimeoutError):
                pass
            if time.time() >= deadline:
                return False
            await asyncio.sleep(5)

    async def _sms_fallback(self, back_to: str) -> bool:
        """SMS connected but no internet: straight back, SMS bad for a while."""
        self._sms_bad_until = time.time() + self._sms_bad_s
        res = await self._net(back_to)
        await self._notify(
            f"⚠ [{self._label}] SMS box has no internet — "
            + (f"back on {LABEL[back_to]}" if res.get("ok")
               else f"going back to {LABEL[back_to]} FAILED: {res.get('error', '?')}")
            + f". SMS skipped for {int(self._sms_bad_s // 60)} min.")
        return bool(res.get("ok"))

    async def move_to(self, path: str, why: str) -> bool:
        """Move for a box reboot (not a rotation): True once online there."""
        cur = self._sync_current()
        if cur == path:
            return True
        res = await self._net(path)
        if not res.get("ok"):
            return False
        if path == "sms" and not await self._internet_ok():
            await self._sms_fallback(cur)
            return False
        self._current = path
        self._trust_self_until = time.time() + 180.0
        await self._notify(f"🔁 [{self._label}] moved {LABEL[cur]} → "
                           f"{LABEL[path]} ({why})")
        return True

    async def _do_rotate(self, cur: str, target: str, reason: str) -> dict:
        log.info("[%s] rotating %s -> %s (%s)", self._label, cur, target, reason)
        res = await self._net(target)
        if res.get("unreachable"):
            await self._notify(f"⚠ [{self._label}] Net rotation {LABEL[cur]} → "
                               f"{LABEL[target]} failed: {res.get('error')}")
            return self.snapshot()

        if res.get("ok") and target == "sms" and not await self._internet_ok():
            back = await self._sms_fallback(cur)
            self._rotations += 1
            self._last_rotate_at = time.time()
            self._current = cur if back else target
            self._trust_self_until = time.time() + 180.0
            st = self._stats[cur]
            st.stint_attempts = 0
            st.stint_limit = self._every
            st.streak = 0
            if self._reboots is not None:
                self._reboots.note_switch()
            return self.snapshot()

        if not res.get("ok"):
            # target is dead / unreachable: skip it for a long penalty stint
            err = res.get("error") or res.get("output") or "?"
            self._dead_until_attempts[target] = self._dead_penalty
            if target == "sms":
                self._sms_bad_until = time.time() + self._sms_bad_s
            st = self._stats[cur]
            st.stint_attempts = 0
            st.stint_limit = max(self._every, 1)
            st.streak = 0
            self._last_rotate_at = time.time()
            await self._notify(
                f"⚠ [{self._label}] {LABEL[target]} is unreachable ({err}). "
                f"Skipping it for {self._dead_penalty} login attempts.")
            return self.snapshot()

        self._rotations += 1
        self._last_rotate_at = time.time()
        self._current = target
        self._trust_self_until = time.time() + 180.0
        st = self._stats[target]
        st.stint_attempts = 0
        st.stint_limit = self._every
        st.streak = 0
        self._stats[cur].streak = 0
        ip = res.get("ip") or ""
        await self._notify(
            f"🔁 [{self._label}] Net rotated {LABEL[cur]} → {LABEL[target]}"
            + (f" ({ip})" if ip else "") + f"\n{reason}")

        if self._reboots is not None:
            self._reboots.note_switch()
        elif self._reboot_on_leave.get(cur):
            # no shared reboot plan: reboot the box we left behind us
            asyncio.create_task(self.reboot_box(
                self._boxes.get(cur, PATH_BOX[cur]),
                why=f"left {LABEL[cur]} for {LABEL[target]}"))
        return self.snapshot()

    # ── router reboots ─────────────────────────────────────────────────────

    async def reboot_box(self, target: str, *, why: str = "manual",
                         force: bool = False) -> dict:
        """Reboot dsl|eth|sms via the device. router_mgr enforces the interlock
        (moves onto a survivor first, never two at once)."""
        t = str(target).lower()
        if t in ("dsl", "wifi", "router", "main"):
            t = "dsl"
        elif t in ("eth", "ethernet", "cable"):
            t = "eth"
        else:
            t = "sms"
        try:
            res = await self._hub.send_command(
                self._dev.id, "action", "router",
                {"op": "reboot", "target": t, "force": force},
                timeout=420.0)
        except RuntimeError as exc:
            res = {"ok": False, "error": str(exc)}
        except TimeoutError:
            res = {"ok": False, "error": "timeout waiting for the device"}
        self._last_reboot[t] = {
            "at": int(time.time()), "ok": bool(res.get("ok")),
            "old_ip": res.get("old_public_ip"), "new_ip": res.get("new_public_ip"),
            "changed": res.get("ip_changed"), "error": res.get("error"),
        }
        name = {"dsl": "DSL router", "eth": "Ethernet router",
                "sms": "SMS box"}[t]
        if res.get("ok"):
            old, new = res.get("old_public_ip"), res.get("new_public_ip")
            if res.get("ip_changed"):
                ipl = f"IP {old} → {new}"
            elif new or old:
                ipl = f"IP {new or old}" + (" (unchanged)" if old and new else "")
            else:
                ipl = "IP n/a (measured from the other path)"
            back = "back" if res.get("came_back") else "STILL DOWN"
            await self._notify(f"♻ [{self._label}] {name} rebooted ({why}) — "
                               f"{back}; {ipl}; device on {res.get('active')}")
        else:
            err = res.get("error") or "?"
            low = str(err).lower()
            if "cooldown" in low or "already running" in low:
                log.info("[%s] %s reboot skipped: %s", self._label, name, err)
            else:
                await self._notify(f"⚠ [{self._label}] {name} reboot failed "
                                   f"({why}): {err}")
        return res

    # ── periodic report ────────────────────────────────────────────────────

    async def _periodic_report(self, path: str) -> None:
        st = self._stats[path]
        hours = (time.time() - st.since) / 3600.0
        rate = (st.done / st.attempts * 100.0) if st.attempts else 0.0
        others = []
        for p in _cycle_after(path, self._paths):
            o = self._stats[p]
            others.append(f"  {LABEL[p]} so far: {o.attempts} attempts, "
                          f"{o.done} done, {o.rejected} rejected")
        await self._notify(
            f"📊 [{self._label}] {LABEL[path]}: {st.attempts} login attempts "
            f"({hours:.1f}h)\n"
            f"  done {st.done} · rejected {st.rejected} · auth "
            f"{st.auth_error} · failed {st.failed}  → {rate:.0f}% ok\n"
            + "\n".join(others) + "\n"
            f"  rotations: {self._rotations}, now on {LABEL[self._current]}")

    # ── background loop: land deferred rotations that missed their idle frame

    async def run(self) -> None:
        log.info("[%s] netrotate started (every=%d streak=%d report=%d "
                 "penalty=%d cooldown=%ss defer=%ss reboot_on_leave=%s)",
                 self._label, self._every, self._reject_streak,
                 self._report_every, self._dead_penalty, self._cooldown,
                 self._max_defer, self._reboot_on_leave)
        while True:
            try:
                if self._pending_since and not self._rotating:
                    # inflight is authoritative (the worker reports it on every
                    # frame, incl. an idle frame); _try_rotate itself applies
                    # the MAX_DEFER_S force cap.
                    await self._try_rotate(force=False)
            except Exception as exc:                    # noqa: BLE001
                log.warning("[%s] netrotate tick error: %s", self._label, exc)
            await asyncio.sleep(self._poll)
