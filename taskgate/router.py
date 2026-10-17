"""router.py — which device a task goes to.

The lsvc task gate holds every routing rule here so main.py stays a relay.
One Router instance, owned by the gate.

Rules (device ids are strings, "1" and "2"):

  main device
    * the "main" swaps every MAIN_SWAP_S (6 h, at 00/06/12/18 local time,
      SWAP_TZ) so the other device rests; LOGIN_PRIMARY is the main in even
      slots

  The rules come from the task's type (task_types.py, rules_for(task)).

  caps (tasks handed to a device and not finished: running or queued there)
    * per cap group, per device (login 5; batch_job commit + check share 3;
      web_payment 1); uncapped types have no limit

  place = fill_main (login, payment)
    * fill the main device up to its cap, then the other one (woken if it's
      off), then hold at the gate until a slot frees
    * the main is woken whenever it's off, so work moves back to it
  place = online_only (lookup)
    * a device that is online now, main first; never wakes a device while
      another one is online
  place = least_busy (action, test)
    * the less busy ready device; ties go to the main
  wake = False (test)
    * never wakes a device; waits for one that is online

Shared:
  * "ready" (computed by the gate) = worker connected with a fresh status
    and a live task loop; browser extensions don't count, because the worker
    opens the browser a task needs itself
  * a device that isn't ready is waited for (online: stale status or a
    frozen loop) or woken (offline); after WAKE_TIMEOUT_S the other device
    is tried
  * when BOTH devices are offline, the main (shift) device is woken; the
    other joins only when the main is full or doesn't come up
  * retry = bounce: a rejected login goes to the other device (woken if
    off), up to LOGIN_MAX_ATTEMPTS or LOGIN_MAX_TOTAL_S; retry =
    other_pc_once: every browser failed on one PC -> the other PC, once
  * a task held longer than HOLD_MAX_S fails back to the caller (the gate
    does that)
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import task_types
from task_types import rules_for

LOGIN_SERVICE = "web_login"
BATCH_JOB = "batch_job"

LOGIN_CAP = task_types.LOGIN_CAP
BATCH_CAP = task_types.BATCH_CAP
LOGIN_PRIMARY = os.getenv("LOGIN_PRIMARY", "1")
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_MAX_TOTAL_S = float(os.getenv("LOGIN_MAX_TOTAL_S", "1800"))
# batch_job: every browser on one PC failed -> the other PC, once
BATCH_MAX_TRIES = int(os.getenv("BATCH_MAX_TRIES", "2"))
WAKE_TIMEOUT_S = float(os.getenv("WAKE_TIMEOUT_S", "240"))
ACK_TIMEOUT_S = float(os.getenv("ACK_TIMEOUT_S", "30"))
MAIN_SWAP_S = float(os.getenv("MAIN_SWAP_S", str(6 * 3600)))
SWAP_TZ = ZoneInfo(os.getenv("SWAP_TZ", "UTC"))
HOLD_MAX_S = float(os.getenv("HOLD_MAX_S", "600"))
QUEUE_MAX_S = float(os.getenv("QUEUE_MAX_S", "300"))
STATUS_STALE_S = float(os.getenv("STATUS_STALE_S", "60"))
LOOP_STALE_S = float(os.getenv("LOOP_STALE_S", "60"))
DISCONNECT_GRACE_S = float(os.getenv("DISCONNECT_GRACE_S", "20"))
WAKE_COOLDOWN_S = float(os.getenv("WAKE_COOLDOWN_S", "180"))

DEVICES = ("1", "2")


def _other(device: str) -> str:
    return "2" if device == "1" else "1"


def main_device(now: float | None = None) -> str:
    """The device that takes quiet-time work. Derived from the clock, so it
    survives gate restarts and both devices get equal turns."""
    now = time.time() if now is None else now
    return LOGIN_PRIMARY if _slot(now) % 2 == 0 else _other(LOGIN_PRIMARY)


def _tz_offset(ts: float) -> float:
    return datetime.fromtimestamp(ts, SWAP_TZ).utcoffset().total_seconds()


def _slot(now: float) -> int:
    """6 h slot index counted on SWAP_TZ wall-clock time."""
    return int((now + _tz_offset(now)) // MAIN_SWAP_S)


def main_until(now: float | None = None) -> float:
    now = time.time() if now is None else now
    boundary = (_slot(now) + 1) * MAIN_SWAP_S      # wall-clock seconds
    guess = boundary - _tz_offset(now)
    return boundary - _tz_offset(guess)            # right across a DST change


def cap_group(task: dict | str) -> str | None:
    """Tasks that share one per-device cap; None = no cap."""
    return rules_for(task).cap_group


def cap_for(task: dict | str) -> int | None:
    return rules_for(task).cap


def run_limit(task: dict | str) -> float:
    return rules_for(task).run_limit


def _cap_groups() -> dict:
    out = {}
    for service, entry in task_types.SERVICES.items():
        for action in (entry if isinstance(entry, dict) else [""]):
            r = rules_for({"service": service, "payload": {"action": action}})
            if r.cap_group:
                out.setdefault(r.cap_group, r)
    return out


def is_batch_commit(task: dict) -> bool:
    action = str((task.get("payload") or {}).get("action") or "")
    return task.get("service") == BATCH_JOB and action == "commit"


def can_bounce(task: dict | str) -> bool:
    return rules_for(task).retry != "none"


class Router:
    def __init__(self) -> None:
        # device id -> when it last went offline (missing = not offline)
        self._offline_since: dict[str, float] = {}
        # task_id -> device id the task is waiting for (booting / waking)
        self._waiting_for: dict[str, str] = {}

    # ── device presence (called by the gate) ──────────────────────────────

    def note_online(self, device: str) -> None:
        self._offline_since.pop(device, None)

    def note_offline(self, device: str) -> None:
        self._offline_since.setdefault(device, time.time())

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "main": main_device(now),
            "main_until": main_until(now),
            "caps": {LOGIN_SERVICE: LOGIN_CAP, BATCH_JOB: BATCH_CAP},
            "cap_groups": {g: r.cap for g, r in _cap_groups().items()},
            "offline_since": dict(self._offline_since),
            "waiting_for": dict(self._waiting_for),
        }

    # ── the decision ───────────────────────────────────────────────────────

    def choose(self, task: dict, ready: set[str], online: set[str],
               load: dict[str, int] | None = None) -> dict:
        """Return {"device": id|None, "wake": id|None, "reason": str}.

        device is where the task goes now (None = hold it). wake is a device
        the gate should boot. ready = devices able to run THIS service now;
        online = devices whose worker is connected; load = tasks each device
        holds in this task's cap group (or in total, for uncapped services).
        """
        load = load or {}
        rules = rules_for(task)
        cap = rules.cap
        now = time.time()
        main = main_device(now)
        tid = task["task_id"]

        # A bounced login avoids the device that just rejected it for a while.
        avoid = task.get("avoid") if now < float(task.get("avoid_until") or 0) else None

        # 1. a ready device with room, in preference order
        if rules.place == "least_busy":
            order = sorted(DEVICES, key=lambda d: (load.get(d, 0), d != main))
        else:
            order = [main, _other(main)]
        for d in order:
            if d == avoid or d not in ready:
                continue
            if cap is not None and load.get(d, 0) >= cap:
                continue
            self._waiting_for.pop(tid, None)
            # Work drifts back to the main: boot it if it's off.
            wake = None
            if (rules.place == "fill_main" and rules.wake and d != main
                    and main not in online):
                wake = main
            return {"device": d, "wake": wake,
                    "reason": f"-> device {d} (main {main}, load "
                              f"{load.get(d, 0)}{'/' + str(cap) if cap else ''})"}

        # 2. already waiting for a device to come up
        tried = task.setdefault("wake_tried", [])
        waiting = self._waiting_for.get(tid)
        if waiting:
            if now - float(task.get("wake_started") or now) < WAKE_TIMEOUT_S:
                return {"device": None, "wake": None,
                        "reason": f"waiting for device {waiting}"}
            self._waiting_for.pop(tid, None)
            if waiting not in tried:
                tried.append(waiting)

        # 3. a device to wait for / wake: one that isn't ready and hasn't
        #    been tried for this task yet
        candidates = [d for d in order if d not in ready and d not in tried]
        if (rules.place == "online_only" and online) or not rules.wake:
            # never wake a device while another one is online (or at all)
            candidates = [d for d in candidates if d in online]
        booting = [d for d in candidates if d in online]
        if booting:
            self._hold_for(task, booting[0])
            return {"device": None, "wake": None,
                    "reason": f"device {booting[0]} is online but not ready yet"}
        if candidates:
            target = candidates[0]
            self._hold_for(task, target)
            why = ("both offline, waking the shift device" if not online
                   else "no room on the online device, waking")
            return {"device": None, "wake": target,
                    "reason": f"{why} device {target}"}

        # 4. every device is full, blocked, or already failed to come up
        if avoid and avoid in ready:
            return {"device": None, "wake": None,
                    "reason": f"waiting (device {avoid} just rejected this)"}
        if tried and not any(d in ready for d in DEVICES):
            # both wake attempts timed out: start over on the next check
            task["wake_tried"] = []
        return {"device": None, "wake": None, "reason": "all devices full or down"}

    def _hold_for(self, task: dict, device: str) -> None:
        if self._waiting_for.get(task["task_id"]) != device:
            task["wake_started"] = time.time()
        self._waiting_for[task["task_id"]] = device

    # ── bounce (a rejected login comes back) ───────────────────────────────

    def bounce(self, task: dict) -> bool:
        """Count one failed attempt that can go elsewhere (a rejected login, or
        a batch_job task every browser on one PC failed). True = route it again
        (the device that failed it is avoided for WAKE_TIMEOUT_S); False =
        budget spent, the caller gets the error."""
        attempts = int(task.get("attempts") or 0) + 1
        task["attempts"] = attempts
        elapsed = time.time() - float(task.get("created") or time.time())
        retry = rules_for(task).retry
        if retry == "none":
            return False
        if retry == "other_pc_once":
            if attempts >= BATCH_MAX_TRIES:
                return False
        elif attempts >= LOGIN_MAX_ATTEMPTS or elapsed >= LOGIN_MAX_TOTAL_S:
            return False
        task["avoid"] = task.get("worker")
        task["avoid_until"] = time.time() + WAKE_TIMEOUT_S
        task["wake_tried"] = []
        self._waiting_for.pop(task["task_id"], None)
        return True

    def forget(self, task_id: str) -> None:
        self._waiting_for.pop(task_id, None)
