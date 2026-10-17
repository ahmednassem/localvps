"""Detect wake / login and bring UI + services online when the laptop opens.

The status display (display_app.py) is launched directly into the user session
here — there is no generic process supervisor any more (process control moved
to the local services side)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Awaitable, Callable

import session
from config import Config
from net_startup import ensure_default_network

log = logging.getLogger("resume")

NotifyWake = Callable[[float], Awaitable[None]]

_WAKE_BOOST_SEC = 300.0   # fast retries for 5 min after lid open / wake
_RETRY_DELAYS = (0, 5, 12, 25, 45, 90)
_HERE = os.path.dirname(os.path.abspath(__file__))


def _pythonw() -> str:
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


class ResumeWatch:
    def __init__(
        self,
        cfg: Config,
        notify_wake: NotifyWake | None = None,
    ) -> None:
        self._cfg = cfg
        self._display_pid: int | None = None
        self._notify_wake = notify_wake
        self._interval = 10.0
        self._last_wall = time.time()
        self._last_usable: set[int] = set()
        self._busy = False
        self._pending_bring_up = False
        self._pending_cloud_notify = False
        self._last_wake_notify = 0.0
        self._wake_boost_until = 0.0

    async def run(self) -> None:
        log.info("resume watch started (interval=%ss)", self._interval)
        # Boot with user already logged in (common after manual power-on).
        await self._schedule_bring_up("startup", gap_s=0.0, boost=True)
        while True:
            interval = 5.0 if time.time() < self._wake_boost_until else self._interval
            # Only the sleep itself may count as "asleep": bring-up chains are
            # awaited below and take minutes, so measuring from the previous
            # tick made every chain look like a wake and re-trigger itself.
            before = time.time()
            await asyncio.sleep(interval)
            now = time.time()
            gap = now - before
            self._last_wall = now

            if gap - interval > 25:
                await self._schedule_bring_up("wake", gap_s=gap, boost=True)

            await self._on_session_change()

            if time.time() < self._wake_boost_until and session.has_usable_session():
                if self._user_services_missing():
                    await self._schedule_bring_up("boost", gap_s=0.0, boost=False)

    async def _on_session_change(self) -> None:
        usable = set(session.usable_session_ids())
        if not usable:
            self._last_usable = usable
            return

        new_sessions = usable - self._last_usable
        self._last_usable = usable

        if new_sessions or self._user_services_missing():
            reason = "login" if new_sessions else "session_retry"
            log.info(
                "user session ready (ids=%s reason=%s need_display=%s need_anydesk=%s)",
                sorted(usable), reason,
                not self._display_ok(), not self._anydesk_ok(),
            )
            await self._schedule_bring_up(reason, gap_s=0.0, boost=True)

    async def _schedule_bring_up(
        self, reason: str, *, gap_s: float, boost: bool,
    ) -> None:
        if boost:
            self._wake_boost_until = max(
                self._wake_boost_until, time.time() + _WAKE_BOOST_SEC)
        if reason == "wake":
            self._pending_cloud_notify = True
        if self._busy:
            self._pending_bring_up = True
            return
        self._busy = True
        try:
            delays = _RETRY_DELAYS if boost else (0,)
            for i, delay in enumerate(delays):
                if delay:
                    await asyncio.sleep(delay)
                if not session.has_usable_session() and reason != "wake":
                    continue
                await self._bring_up_once(
                    f"{reason}{f'+{delay}s' if delay else ''}", gap_s=gap_s,
                    restart_display=(i == 0))
                if not self._user_services_missing():
                    log.info("laptop open: all user services OK after %s", reason)
                    break
        except Exception as exc:                         # noqa: BLE001
            log.warning("%s bring-up failed: %s", reason, exc)
        finally:
            self._busy = False
            if self._pending_bring_up:
                self._pending_bring_up = False
                asyncio.create_task(
                    self._schedule_bring_up("pending", gap_s=0.0, boost=True))

    async def _bring_up_once(self, tag: str, *, gap_s: float,
                             restart_display: bool = True) -> None:
        res = await ensure_default_network(self._cfg, reason=tag, patch_status=True)
        log.info("%s network: ok=%s net=%s",
                 tag, res.get("ok"), res.get("net") or res.get("source"))
        await self._ensure_display(restart=restart_display)
        await self._ensure_anydesk()
        if self._pending_cloud_notify and self._notify_wake:
            await self._maybe_notify_wake(gap_s if gap_s > 0 else 1.0)

    async def _maybe_notify_wake(self, gap_s: float) -> None:
        now = time.time()
        if now - self._last_wake_notify < 90:
            return
        try:
            await self._notify_wake(gap_s)  # type: ignore[misc]
            self._last_wake_notify = now
            self._pending_cloud_notify = False
            log.info("wake cloud notification sent")
        except Exception as exc:                         # noqa: BLE001
            log.warning("wake cloud notify failed: %s", exc)

    def _display_ok(self) -> bool:
        pid = self._display_pid
        if pid is None:
            return False
        try:
            import psutil
            import win32ts
            p = psutil.Process(pid)
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return False
            # psutil has no session_id() on Windows; ask the terminal service.
            return int(win32ts.ProcessIdToSessionId(pid)) != 0
        except Exception:                                # noqa: BLE001
            return False

    def _anydesk_ok(self) -> bool:
        if not self._cfg.anydesk_watch_enabled:
            return True
        if not session.has_usable_session():
            return False
        import anydesk_mgr
        return anydesk_mgr.is_running() and anydesk_mgr.is_elevated()

    def _user_services_missing(self) -> bool:
        if not session.has_usable_session():
            return False
        return not self._display_ok() or not self._anydesk_ok()

    async def _ensure_display(self, *, restart: bool = False) -> None:
        """Direct launch of display_app.py into the user desktop session."""
        if not session.has_usable_session():
            log.debug("display waiting for user session")
            return
        if self._display_ok() and not restart:
            return
        if self._display_pid is not None:
            try:
                import psutil
                psutil.Process(self._display_pid).kill()
            except Exception:                            # noqa: BLE001
                pass
            self._display_pid = None
            await asyncio.sleep(0.5)
        cmd = f'"{_pythonw()}" display_app.py'
        res = session.launch_in_session(cmd, cwd=_HERE)
        if res.get("ok"):
            self._display_pid = res.get("pid")
            log.info("display started (pid=%s)", self._display_pid)
        else:
            log.warning("display failed: %s", res.get("error"))

    async def _ensure_anydesk(self) -> None:
        if not self._cfg.anydesk_watch_enabled:
            return
        if not session.has_usable_session():
            return
        try:
            from anydesk_watchdog import ensure_running
            res = await ensure_running(self._cfg)
            if not res.get("ok") and not res.get("skipped"):
                log.warning("AnyDesk ensure failed: %s",
                            res.get("error") or res.get("output"))
        except Exception as exc:                         # noqa: BLE001
            log.warning("AnyDesk check failed: %s", exc)
