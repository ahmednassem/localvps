"""scheduler.py — cron jobs on the VPS (not on the laptop).

Reads schedule.json and runs tasks on a timer: send commands to the laptop agent,
control the smart plug, or push a Telegram notification.

Job schema (schedule.json):
  {
    "jobs": [
      {
        "name": "nightly_reboot",
        "cron": "0 3 * * *",
        "enabled": true,
        "task": {
          "type": "agent",
          "action": "reboot"
        }
      },
      {
        "name": "morning_status",
        "cron": "0 8 * * *",
        "task": {"type": "notify", "message": "Daily VPS check"}
      },
      {
        "name": "plug_cycle_weekly",
        "cron": "0 4 * * 0",
        "task": {"type": "plug", "op": "cycle"}
      },
      {
        "name": "morning_open",
        "cron": "0 9 * * *",
        "task": {
          "type": "open",
          "only_if_offline": true,
          "wait_seconds": 120,
          "then": {"type": "agent", "action": "anydesk", "args": {"op": "start"}}
        }
      }
    ]
  }

Cron format: minute hour day month weekday.
Times use SCHEDULE_TZ from .env (default UTC). Example: Europe/Athens = UTC+3 in summer,
so "0 9 * * *" runs at 9:00 AM local time even if the server clock is UTC.

Optional per job:
  "if_online": true   — only run when the device recently posted status
  "device": 1         — which device the job targets (default 1)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from croniter import croniter

from hub import Hub
from services import DeviceServices

log = logging.getLogger("scheduler")

Notify = Callable[[str], Awaitable[None]]


class JobRunner:
    def __init__(self, hub: Hub, services: dict[int, DeviceServices],
                 notify: Notify, schedule_path: str,
                 schedule_tz: str = "UTC",
                 tick_grace: float = 300.0) -> None:
        self._hub = hub
        self._services = services
        self._notify = notify
        self._path = schedule_path
        self._tick_grace = tick_grace
        try:
            self._tz = ZoneInfo(schedule_tz)
        except Exception:
            log.warning("invalid SCHEDULE_TZ=%r — using UTC", schedule_tz)
            self._tz = ZoneInfo("UTC")
        self._last_run: dict[str, float] = {}

    def _load(self) -> list[dict]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            return [j for j in data.get("jobs", []) if j.get("enabled", True)]
        except Exception as exc:                    # noqa: BLE001
            log.error("failed to read schedule: %s", exc)
            return []

    async def run(self) -> None:
        log.info("scheduler started (%s, tz=%s, grace=%ss)",
                 self._path, self._tz.key, self._tick_grace)
        while True:
            try:
                await self._tick()
            except Exception as exc:                # noqa: BLE001
                log.warning("scheduler tick error: %s", exc)
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        now_ts = time.time()
        now_local = datetime.now(self._tz)
        for job in self._load():
            name = job.get("name", "?")
            cron = job.get("cron", "")
            if not cron:
                continue
            try:
                itr = croniter(cron, now_local)
                due_at = itr.get_prev(float)
            except Exception as exc:                # noqa: BLE001
                log.warning("bad cron for %s: %s", name, exc)
                continue
            last = self._last_run.get(name, 0)
            if due_at <= last:
                continue
            if now_ts - due_at > self._tick_grace:
                continue
            self._last_run[name] = due_at
            device = self._job_device(job)
            if device is None:
                log.warning("skip job %s — unknown device %r",
                            name, job.get("device"))
                continue
            if job.get("if_online") and not self._hub.device(device).online():
                log.info("skip job %s — device %d offline", name, device)
                continue
            await self._execute(name, device, job.get("task", {}))

    def _job_device(self, job: dict) -> int | None:
        try:
            device = int(job.get("device", 1))
        except (TypeError, ValueError):
            return None
        return device if device in self._services else None

    async def _execute(self, name: str, device: int, task: dict) -> None:
        typ = task.get("type", "")
        svc = self._services[device]
        log.info("running job %s (%s, device %d)", name, typ, device)
        try:
            if typ == "agent":
                action = task.get("action", "")
                args = task.get("args", {}) or {}
                timeout = float(task.get("timeout", 120))
                res = await self._hub.send_command(device, "action", action,
                                                   args, timeout=timeout)
                msg = f"[schedule:{name}] {action} → ok={res.get('ok')}"
                if res.get("output"):
                    msg += f"\n{res['output'][:500]}"
                await self._notify(msg)
            elif typ == "plug":
                op = task.get("op", "")
                if not svc.plug.configured:
                    await self._notify(f"[schedule:{name}] plug not configured")
                    return
                res = await getattr(svc.plug, op)()
                await self._notify(f"[schedule:{name}] plug {op} → {res}")
            elif typ == "open":
                await self._run_open(name, device, task)
            elif typ == "notify":
                await self._notify(task.get("message", f"[schedule:{name}]"))
            elif typ == "shell":
                cmd = task.get("cmd", "")
                res = await self._hub.send_command(device, "shell", "shell",
                                                   {"cmd": cmd},
                                                   timeout=float(task.get("timeout", 120)))
                await self._notify(f"[schedule:{name}] shell → ok={res.get('ok')}")
            else:
                log.warning("unknown task type %s in job %s", typ, name)
        except RuntimeError:
            await self._notify(f"[schedule:{name}] FAILED — core offline")
        except TimeoutError:
            await self._notify(f"[schedule:{name}] FAILED — timeout")
        except Exception as exc:                    # noqa: BLE001
            log.exception("job %s failed", name)
            await self._notify(f"[schedule:{name}] ERROR: {exc}")

    async def _run_open(self, name: str, device: int, task: dict) -> None:
        """Wake a device (plug cycle) then optionally run a follow-up task."""
        svc = self._services[device]
        dev = self._hub.device(device)

        only_if_offline = task.get("only_if_offline", False)
        need_wake = (
            dev.intentional_offline
            or not dev.online()
            or not only_if_offline
        )
        if only_if_offline and not need_wake:
            await self._notify(
                f"[schedule:{name}] open — already online, skipping wake cycle")
        else:
            res = await svc.vps.online(notify=True)
            if not res.get("ok"):
                await self._notify(
                    f"[schedule:{name}] open FAILED — {res.get('error', '?')}")
                return
            wait_s = float(task.get("wait_seconds", 90))
            if wait_s > 0:
                await asyncio.sleep(wait_s)

        then = task.get("then")
        if then:
            await self._execute(f"{name}:then", device, then)
