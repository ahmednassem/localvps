"""cluster_local.py — lead-node fallback when the VPS can't be reached (phase 3).

the VPS runs the power rules. When neither device has had its VPS link for
LEAD_TAKEOVER_S, the lead device runs a few of them at home:

  - idle shutdown of the other device after LEAD_IDLE_S without the VPS (no
    task can arrive then); the lead itself stays up to keep watching;
  - its plug cut once that device is down: peer link gone CUT_SETTLE_S and
    the plug draws under PLUG_MIN_WATTS, or CUT_MAX_WAIT_S after the order;
  - its own plug on when it runs on battery with the plug off (self-boot).

The lead is the shift device (device 2 in 00-06 and 12-18 local time) while
the peer link is up, otherwise whichever device is still running. A device
only obeys a peer "shutdown" while it has no VPS link itself.

Every action goes to lead_log.jsonl; when the VPS is back the log is sent as
one event and moved to lead_log.sent.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("lead")

_HERE = Path(__file__).resolve().parent
LOG_PATH = _HERE / "lead_log.jsonl"
SENT_PATH = _HERE / "lead_log.sent.jsonl"

POLL_S = 30.0
LEAD_TAKEOVER_S = float(os.environ.get("LEAD_TAKEOVER_S", "300") or 300)
LEAD_IDLE_S = float(os.environ.get("LEAD_IDLE_S", "1800") or 1800)
CUT_SETTLE_S = 60.0
CUT_MAX_WAIT_S = 300.0
CUT_GIVE_UP_S = 900.0


def _local_tz():
    lib = str(_HERE / "lib")
    if lib not in sys.path:
        sys.path.append(lib)
    try:
        return ZoneInfo("UTC")
    except Exception:                                # noqa: BLE001
        return timezone.utc


TZ = _local_tz()

LEAD: "LeadFallback | None" = None


def snapshot() -> dict:
    return LEAD.status() if LEAD else {}


SHIFT_FIRST = int(os.environ.get("SHIFT_FIRST", "2") or 2)


def shift_device(now: datetime | None = None) -> int:
    """SHIFT_FIRST is main in 00-06 and 12-18, the other in 06-12 and 18-24."""
    h = (now or datetime.now(TZ)).hour
    other = 1 if SHIFT_FIRST == 2 else 2
    return SHIFT_FIRST if (0 <= h < 6 or 12 <= h < 18) else other


class LeadFallback:
    def __init__(self, device_id: int, link, *,
                 vps_ok: Callable[[], bool],
                 notify: Callable[[str], Awaitable[None]],
                 plug: Callable[[str, int], Awaitable[dict]],
                 on_battery: Callable[[], bool | None],
                 shutdown: Callable[[], Awaitable[dict]],
                 clock: Callable[[], float] = time.time,
                 shift: Callable[[], int] = shift_device,
                 log_path: Path = LOG_PATH, sent_path: Path = SENT_PATH) -> None:
        self.device_id = device_id
        self.peer_id = 2 if device_id == 1 else 1
        self._link = link
        self._vps_ok = vps_ok
        self._notify = notify
        self._plug = plug
        self._on_battery = on_battery
        self._shutdown = shutdown
        self._clock = clock
        self._shift = shift
        self._log_path = log_path
        self._sent_path = sent_path
        self.lost_since: float | None = None
        self.active = False
        self._peer_off_at: float | None = None
        self._peer_down_since: float | None = None
        self._peer_cut_done = False
        self._own_plug_on_at = 0.0
        link.on("shutdown", self._on_shutdown_order)

    # ── state ─────────────────────────────────────────────────────────────

    def _peer_status(self) -> dict:
        return self._link.status()

    def _nobody_has_vps(self) -> bool:
        if self._vps_ok():
            return False
        st = self._peer_status()
        return not (st.get("connected") and (st.get("peer_state") or {}).get("vps_link"))

    def is_lead(self) -> bool:
        if not self._peer_status().get("connected"):
            return True
        return self._shift() == self.device_id

    def status(self) -> dict:
        now = self._clock()
        return {"active": self.active, "lead": self.is_lead(),
                "vps_lost_s": round(now - self.lost_since) if self.lost_since else None,
                "peer_off_ordered": self._peer_off_at is not None,
                "peer_cut_done": self._peer_cut_done}

    # ── loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        log.info("lead fallback armed (takeover after %ss, idle %ss)",
                 LEAD_TAKEOVER_S, LEAD_IDLE_S)
        while True:
            try:
                await self.tick()
            except Exception as exc:                 # noqa: BLE001
                log.warning("lead tick error: %s", exc)
            await asyncio.sleep(POLL_S)

    async def tick(self) -> None:
        now = self._clock()
        if not self._nobody_has_vps():
            if self.active:
                log.info("the VPS is back — handing back")
            self.active = False
            self.lost_since = None
            self._peer_off_at = self._peer_down_since = None
            self._peer_cut_done = False
            if self._vps_ok():
                await self._replay()
            return
        if self.lost_since is None:
            self.lost_since = now
        lost = now - self.lost_since
        if lost < LEAD_TAKEOVER_S or not self.is_lead():
            self.active = False
            return
        if not self.active:
            self.active = True
            self._record("takeover", f"no VPS for {int(lost)}s, device "
                         f"{self.device_id} runs the power rules")
        await self._own_plug_rule()
        await self._peer_idle_rule(lost)
        await self._peer_cut_rule()

    # ── rules ─────────────────────────────────────────────────────────────

    async def _own_plug_rule(self) -> None:
        if self._on_battery() is not True:
            return
        if self._clock() - self._own_plug_on_at < 300:
            return
        st = await self._plug("status", self.device_id)
        if st.get("ok") and not st.get("socket"):
            self._own_plug_on_at = self._clock()
            res = await self._plug("on", self.device_id)
            self._record("own_plug_on", "on battery with the plug off",
                         res.get("ok"), res.get("error"))

    async def _peer_idle_rule(self, lost: float) -> None:
        if self._peer_off_at is not None or lost < LEAD_IDLE_S:
            return
        if not self._peer_status().get("connected"):
            return
        res = await self._link.request("shutdown", {"from": self.device_id,
                                                    "reason": "idle, no VPS"})
        self._peer_off_at = self._clock()
        self._record("peer_shutdown", f"device {self.peer_id} idle "
                     f"{int(lost // 60)} min without the VPS", res.get("ok"),
                     res.get("error"))

    async def _peer_cut_rule(self) -> None:
        if self._peer_off_at is None or self._peer_cut_done:
            return
        now = self._clock()
        if self._peer_status().get("connected"):
            if self._peer_down_since is not None:
                self._peer_cut_done = True
                self._record("peer_cut_skipped", "peer link came back")
            elif now - self._peer_off_at >= CUT_GIVE_UP_S:
                self._peer_cut_done = True
                self._record("peer_cut_skipped", "never went down")
            return
        if self._peer_down_since is None:
            self._peer_down_since = now
        if now - self._peer_down_since < CUT_SETTLE_S:
            return
        if now - self._peer_off_at < CUT_MAX_WAIT_S:
            st = await self._plug("status", self.peer_id)
            if not st.get("ok"):
                return
            min_w = float(os.environ.get("PLUG_MIN_WATTS", "5") or 5)
            if st.get("socket") and float(st.get("watts") or 0) > min_w:
                return
        res = await self._plug("off", self.peer_id)
        self._peer_cut_done = bool(res.get("ok"))
        self._record("peer_plug_off", f"device {self.peer_id} down",
                     res.get("ok"), res.get("error"))

    async def _on_shutdown_order(self, body: dict) -> dict:
        if self._vps_ok():
            return {"ok": False, "error": "refused: the VPS link is up"}
        self._record("shutdown_by_peer", f"ordered by device {body.get('from')}: "
                     f"{body.get('reason', '')}")
        asyncio.get_running_loop().call_later(
            2, lambda: asyncio.ensure_future(self._shutdown()))
        return {"ok": True}

    # ── log ───────────────────────────────────────────────────────────────

    def _record(self, action: str, detail: str, ok: bool | None = True,
                error: str | None = None) -> None:
        entry = {"ts": self._clock(), "device": self.device_id,
                 "action": action, "detail": detail, "ok": bool(ok)}
        if error:
            entry["error"] = error
        log.info("lead: %s — %s%s", action, detail, "" if ok else f" (FAILED: {error})")
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    async def _replay(self) -> None:
        if not self._log_path.exists():
            return
        lines = [json.loads(ln) for ln in
                 self._log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            self._log_path.unlink(missing_ok=True)
            return
        tz = TZ
        text = "\n".join(
            f"{datetime.fromtimestamp(e['ts'], tz):%H:%M} {e['action']}: {e['detail']}"
            + ("" if e.get("ok") else f" (FAILED: {e.get('error', '?')})")
            for e in lines[-20:])
        await self._notify(f"🏠 Device {self.device_id} ran the power rules while "
                           f"the VPS was unreachable ({len(lines)} actions):\n{text}")
        with self._sent_path.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(e) + "\n" for e in lines))
        self._log_path.unlink(missing_ok=True)
