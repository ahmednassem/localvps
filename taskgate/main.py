"""VPS gate.

A pure forwarder. Other processes on the VPS POST tasks here in a fixed format:

    POST /task
    {
        "service": "service1",
        "callback_url": "http://127.0.0.1:9000/result",
        "payload": { ...task JSON, anything the service needs... }
    }

The gate forwards the task straight to the local server over a WebSocket, keeps
only a task_id -> callback_url mapping, and when the server answers it forwards
the result to callback_url. No queue, no dispatching, no limits - all of that
lives on the local-server side.

On top of the relay it exposes a loopback-only admin HTTP API (X-Admin-Key)
used by the backend to show the status page: status, task history,
run a test task, retry/cancel, control browsers, read tokens.

Failure handling:
  - server offline when a task arrives -> hold it, warn on Telegram, forward on reconnect
  - server answers done                -> deliver result to callback_url
  - server answers error/timeout       -> warn, deliver the error to callback_url
                                          immediately (the caller owns the failure
                                          flow; the /ext/* backup pool is NOT used)
  - task submitted with target=backup  -> parked straight in the backup extension
                                          pool (/ext/*); an extension claims it and
                                          posts the result, or the TTL sweeper
                                          delivers an error. Never reaches the local
                                          server, not even through an admin retry
                                          (retry just re-arms it in the pool)

Run:  python main.py   (or: uvicorn main:app --host 127.0.0.1 --port 8102)
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

import config
import router
import stats
import task_types
import telegram_bot
from task_types import rules_for

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gate")

# Times shown on Telegram are local time; the server clock is UTC.
_TZ = ZoneInfo(os.getenv("GATE_TZ", "UTC"))


def _local(ts: float | None = None, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, _TZ).strftime(fmt)

# Task states
HELD = "held"                    # server offline, waiting to be forwarded
SENT_HP = "sent_hp"              # forwarded to server, waiting for its result
BACKUP_AVAILABLE = "backup_available"   # server failed it, waiting for an extension to poll
BACKUP_CLAIMED = "backup_claimed"       # an extension took it, running it

# task_id -> {task_id, service, callback_url, payload, state, hp_error, created,
#             claimed_at, sent_at, finished_at}
tasks: dict[str, dict] = {}

# Finished tasks (done/failed/cancelled) kept for the admin page.
history: deque = deque(maxlen=config.HISTORY_MAX)

class Worker:
    """One connected local server (a laptop). Keyed by its device id so two
    laptops can work at once; each carries its own browser snapshot."""

    def __init__(self, worker_id: str, ws: WebSocket) -> None:
        self.id = worker_id
        self.ws = ws
        self.connected_at = time.time()
        self.browsers: dict = {}   # name -> {running, ext_connected}
        self.last_status: float = 0.0          # when the last status frame came
        self.loop_age: float | None = None     # worker task loop heartbeat age
        self.task_ids: set[str] | None = None  # tasks the worker holds (None = old worker)
        self.version: str | None = None        # worker code hash
        self.link: dict | None = None          # {link: domain|ip, host, on_ip_s}


# device id -> Worker. Empty = no local server online.
_workers: dict[str, Worker] = {}
# which device a task goes to, and when a device last went offline
_router = router.Router()
# device id -> when the gate last asked the power gate to boot it
_last_wake: dict[str, float] = {}
_last_main: str | None = None
_gate_started: float = time.time()
_http: httpx.AsyncClient | None = None
# Dedicated client for the local-server power gate: it serves TLS with a
# self-signed cert on the direct IP, so loopback calls skip verification.
_power_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http, _power_http, _gate_started
    _http = httpx.AsyncClient(timeout=config.CALLBACK_TIMEOUT)
    _power_http = httpx.AsyncClient(timeout=config.CALLBACK_TIMEOUT,
                                    verify=False)
    _gate_started = time.time()
    _load_state()
    restored = _restore_tasks()
    queued = _outbox_load()
    polling = asyncio.create_task(telegram_bot.run_polling(_status_text, _find_ref))
    sweeper = asyncio.create_task(_ttl_sweeper())
    supervisor = asyncio.create_task(_supervisor())
    outbox = asyncio.create_task(_outbox_loop())
    daily = asyncio.create_task(_daily_stats_loop())
    await telegram_bot.notify(
        "Gate started." + (f" Restored {restored} live task(s)." if restored else "")
        + (f" {queued} result(s) waiting in the outbox." if queued else ""))
    try:
        yield
    finally:
        polling.cancel()
        sweeper.cancel()
        supervisor.cancel()
        outbox.cancel()
        daily.cancel()
        _persist_tasks()
        await _http.aclose()
        if _power_http is not None:
            await _power_http.aclose()


app = FastAPI(title="VPS Task Gate", lifespan=lifespan)


class TaskIn(BaseModel):
    service: str = Field(min_length=1)
    callback_url: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    # "hp" (default): forward to the local server. "backup": skip the local
    # server entirely — park the task in the backup extension pool (/ext/*)
    # for a human-driven browser extension to claim and finish.
    target: str = Field(default="hp", pattern="^(hp|backup)$")


def _worker_connected() -> bool:
    return len(_workers) > 0


def _inflight(worker_id: str) -> int:
    """How many tasks are currently sent to (running on) this device."""
    return sum(1 for t in tasks.values()
               if t["state"] == SENT_HP and t.get("worker") == worker_id)


def _worker_healthy(w: "Worker") -> bool:
    """Fresh status and a task loop that is still turning."""
    if time.time() - w.last_status > router.STATUS_STALE_S:
        return False
    return w.loop_age is None or w.loop_age < router.LOOP_STALE_S


def _ready_ids(service: str | None = None) -> set[str]:
    """Devices that can take work now: worker connected and healthy. The
    worker opens the browser a task needs itself, so a closed browser (no
    extension connected yet) doesn't make a device unready."""
    return {i for i, w in _workers.items() if _worker_healthy(w)}


def _load(task: dict | str) -> dict[str, int]:
    """Tasks each device holds in this task's cap group (all its tasks,
    for an uncapped type)."""
    group = router.cap_group(task)
    out: dict[str, int] = {}
    for t in tasks.values():
        if t["state"] != SENT_HP or not t.get("worker"):
            continue
        if group is not None and router.cap_group(t) != group:
            continue
        out[t["worker"]] = out.get(t["worker"], 0) + 1
    return out


def _load_by_type() -> dict[str, dict[str, int]]:
    """Tasks each device holds, per task type (running or queued there)."""
    out: dict[str, dict[str, int]] = {name: {} for name in task_types.TYPES}
    for t in tasks.values():
        if t["state"] != SENT_HP or not t.get("worker"):
            continue
        per = out.setdefault(rules_for(t).type, {})
        per[t["worker"]] = per.get(t["worker"], 0) + 1
    return out


def _merged_browsers() -> dict:
    """Union of every device's browser snapshot (running/ext up on ANY device)
    — keeps the admin status page working with two workers."""
    merged: dict = {}
    for w in _workers.values():
        for name, info in w.browsers.items():
            m = merged.setdefault(name, {"running": False, "ext_connected": False})
            m["running"] = m["running"] or bool(info.get("running"))
            m["ext_connected"] = m["ext_connected"] or bool(info.get("ext_connected"))
    return merged


# ── power gate (night idle shutdown / wake-on-task) ───────────────────────────
# Every task/result pings the local vps power gate so it knows the second PC
# is earning its power overnight; when a task arrives while the worker is
# offline we ask the power gate to boot the machine (the task is HELD here and
# flushed as soon as the worker reconnects).

def _power_gate_enabled() -> bool:
    return bool(config.POWER_GATE_URL and config.POWER_GATE_KEY)


async def _power_gate_get(path: str, timeout: float = 10.0) -> dict | None:
    if not _power_gate_enabled() or _power_http is None:
        return None
    try:
        resp = await _power_http.get(f"{config.POWER_GATE_URL}{path}",
                                     headers={"X-Admin-Key": config.POWER_GATE_KEY},
                                     timeout=timeout)
        return resp.json()
    except Exception as exc:
        log.warning("power gate %s failed: %s", path, exc)
        return None


async def _rotation_counts() -> dict:
    out = {}
    for d in ("1", "2"):
        r = await _power_gate_get(f"/api/{d}/net/status")
        if isinstance(r, dict) and r.get("ok"):
            out[d] = {"enabled": r.get("enabled"), "switches": r.get("rotations", 0),
                      "logins": sum(int(p.get("attempts") or 0)
                                    for p in (r.get("paths") or {}).values())}
    return out


async def _daily_stats_loop() -> None:
    """Yesterday's stats at 00:05 local time, once a day."""
    sent_for = stats.day_of(time.time() - 86400)
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            yesterday = stats.day_of(now - 86400)
            local = datetime.fromtimestamp(now, stats.TZ)
            if yesterday != sent_for and (local.hour, local.minute) >= (0, 5):
                sent_for = yesterday
                await telegram_bot.notify(
                    stats.summary_text(yesterday, await _rotation_counts()))
        except Exception:
            log.exception("daily stats failed")


async def _power_gate_post(path: str, timeout: float,
                           body: dict | None = None) -> dict | None:
    if not _power_gate_enabled() or _power_http is None:
        return None
    try:
        resp = await _power_http.post(
            f"{config.POWER_GATE_URL}{path}",
            headers={"X-Admin-Key": config.POWER_GATE_KEY},
            json=body,
            timeout=timeout,
        )
        return resp.json()
    except Exception as exc:
        log.warning("power gate %s failed: %s", path, exc)
        return None


def _net_report_bg(message: dict, worker: "Worker") -> None:
    """Forward a worker netreport frame (login attempt / idle) to the localvps
    gate's IP-rotation policy. Fire-and-forget, like the night keepalive.

    Tagged with the reporting device id so the localserver gate counts login
    attempts / rotation per device (each laptop rides its own uplink)."""
    if not _power_gate_enabled():
        return
    body = {k: v for k, v in message.items() if k != "type"}
    body.setdefault("device", int(worker.id) if worker.id.isdigit() else worker.id)
    asyncio.create_task(_power_gate_post("/api/net/report", 10.0, body))


def _power_keepalive_bg(device: str | None) -> None:
    """Tell the local server gate that THIS device just did work, so its idle
    timer resets. Without the device id the call lands on device 1 only."""
    if _power_gate_enabled() and device:
        asyncio.create_task(
            _power_gate_post("/api/night/keepalive", 10.0, {"device": device}))


def _power_wake_bg(device: str | None) -> None:
    """Boot one device. The task that needs it stays HELD and goes out the
    moment that device's worker reconnects."""
    if not _power_gate_enabled() or not device:
        return

    async def _go() -> None:
        # the wake may plug-cycle (off -> wait -> on), give it time
        res = await _power_gate_post("/api/night/wake", 45.0, {"device": device})
        if res and res.get("waking") and not res.get("cooldown"):
            log.info("power gate is waking device %s for a held task", device)
            telegram_bot.notify_bg(f"Waking device {device} for a held task.")

    asyncio.create_task(_go())


# ── task cards (one Telegram message per task, edited as it moves) ────────────

_STATE_FILE = os.getenv("GATE_STATE_FILE",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "state.json"))
_task_seq: int = 0
# The task reference people use: local date + that day's number, "29/12-5".
REF_TZ = ZoneInfo(os.getenv("REF_TZ", "UTC"))
_ref_day: str = ""
_ref_seq: int = 0


def _load_state() -> None:
    global _task_seq, _ref_day, _ref_seq
    try:
        with open(_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _task_seq = int(data.get("task_seq", 0))
        _ref_day = str(data.get("ref_day") or "")
        _ref_seq = int(data.get("ref_seq", 0))
    except (OSError, ValueError, AttributeError):
        _task_seq, _ref_day, _ref_seq = 0, "", 0


def _save_state() -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"task_seq": _task_seq, "ref_day": _ref_day,
                       "ref_seq": _ref_seq}, fh)
    except OSError as exc:
        log.warning("could not save gate state: %s", exc)


def _new_ref(now: float | None = None) -> str:
    """Next reference for today (local): one counter for every task, reset
    at 00:00 local. Call and save without awaiting in between."""
    global _ref_day, _ref_seq
    day = datetime.fromtimestamp(time.time() if now is None else now, REF_TZ).date()
    if day.isoformat() != _ref_day:
        _ref_day, _ref_seq = day.isoformat(), 0
    _ref_seq += 1
    return f"{day.day}/{day.month}-{_ref_seq}"


def _ref(task: dict) -> str:
    return task.get("ref") or f"#{task.get('num', '?')}"


def _task_meta(task: dict) -> str:
    """One short line about the task. Passwords and cookies never appear."""
    p = task.get("payload") or {}
    bits = []
    if p.get("job_id"):
        bits.append(f"job {p['job_id']}")
    if p.get("action"):
        bits.append(str(p["action"]))
    email = str(p.get("email") or p.get("account_email") or "").strip()
    if email:
        bits.append(email)
    if p.get("username"):
        bits.append("user " + str(p["username"]))
    if p.get("plan"):
        bits.append("plan " + str(p["plan"]))
    return " | ".join(bits)


def _card_text(task: dict) -> str:
    head = (f"{_ref(task)}  {task['service']} | "
            f"{rules_for(task).type}")
    meta = _task_meta(task)
    lines = task.get("card_lines") or []
    return "\n".join([head, meta] + lines if meta else [head] + lines)


async def _card_add(task: dict, line: str) -> None:
    """Append one line to the task's card and update its Telegram message."""
    stamp = _local()
    task.setdefault("card_lines", []).append(f"{stamp}  {line}")
    # Telegram caps a message at 4096 chars; keep the head and the tail.
    while len(_card_text(task)) > 4000 and len(task["card_lines"]) > 2:
        task["card_lines"].pop(0)
    if not rules_for(task).card:
        return
    text = _card_text(task)
    if task.get("card_msg_id"):
        await telegram_bot.edit(task["card_msg_id"], text)
    else:
        task["card_msg_id"] = await telegram_bot.send(text)


def _status_text() -> str:
    counts = {HELD: 0, SENT_HP: 0, BACKUP_AVAILABLE: 0, BACKUP_CLAIMED: 0}
    for t in tasks.values():
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    if _workers:
        lines = []
        for w in sorted(_workers.values(), key=lambda x: x.id):
            up = ", ".join(
                f"{b}{'*' if v.get('ext_connected') else ''}"
                for b, v in w.browsers.items() if v.get("running")
            ) or "none"
            lines.append(f"  device {w.id}: browsers {up}, in flight {_inflight(w.id)}")
        servers = f"{len(_workers)} online\n" + "\n".join(lines)
    else:
        servers = "OFFLINE"
    return (
        "Task-relay gate\n"
        f"local servers: {servers}\n"
        f"held (all servers offline): {counts[HELD]}\n"
        f"in flight: {counts[SENT_HP]}\n"
        f"backup pool: {counts[BACKUP_AVAILABLE] + counts[BACKUP_CLAIMED]}"
    )


@app.get("/health")
async def health():
    counts: dict[str, int] = {}
    for t in tasks.values():
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {
        "status": "ok",
        "worker_connected": _worker_connected(),
        "worker_count": len(_workers),
        "tasks": len(tasks),
        "by_state": counts,
    }


# ── inbound tasks (trusted / internal, no auth) ───────────────────────────────

@app.post("/task")
async def submit_task(task_in: TaskIn):
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "service": task_in.service,
        "callback_url": task_in.callback_url,
        "payload": task_in.payload,
        "target": task_in.target,
        "state": HELD,
        "hp_error": None,
        "worker": None,
        "created": time.time(),
        "sent_at": None,
        "finished_at": None,
        "claimed_at": None,
    }
    tasks[task_id] = task

    global _task_seq
    _task_seq += 1
    task["num"] = _task_seq
    task["ref"] = _new_ref()
    _save_state()
    log.info("task %s received: ref %s (%s)", task_id, task["ref"], task["service"])

    if task_in.target == "backup":
        # Straight to the backup extension pool — never touches the local
        # server. The TTL sweeper delivers an error if no extension finishes
        # it in time.
        task["state"] = BACKUP_AVAILABLE
        await _card_add(task, "for the BACKUP extension — open it to run this")
        return {"task_id": task_id, "ref": task["ref"], "status": "accepted"}

    await _card_add(task, "received")
    if _waiting_ahead(task):
        await _route_held(router.cap_group(task))
    else:
        await _route(task)
    _persist_tasks()
    return {"task_id": task_id, "ref": task["ref"], "status": "accepted"}


def _held(group: str | None = None) -> list[dict]:
    return [t for t in tasks.values()
            if t["state"] == HELD and not t.get("failing")
            and t.get("target") != "backup"
            and (group is None or router.cap_group(t) == group)]


def _waiting_ahead(task: dict) -> bool:
    """A held task that shares this one's cap and ranks before it (higher
    type priority, or same type and older): it gets the next slot first."""
    group = router.cap_group(task)
    if group is None:
        return False
    mine = (task_types.priority(task), float(task.get("created") or 0))
    return any(t is not task and
               (task_types.priority(t), float(t.get("created") or 0)) <= mine
               for t in _held(group))


async def _route_held(group: str | None = None) -> None:
    """Offer slots to held tasks in priority order (login, payment, action,
    lookup, test; oldest first within a type)."""
    for t in task_types.pick_order(_held(group)):
        if t["state"] == HELD and not t.get("failing"):
            await _route(t)


async def _route(task: dict) -> None:
    """Decide where a task goes (router.py) and send or hold it."""
    if task.get("target") == "backup":
        return
    service = task["service"]
    decision = _router.choose(task, _ready_ids(service), set(_workers.keys()),
                              _load(task))
    reason = decision.get("reason") or ""
    wake = decision.get("wake")
    if wake:
        await _wake(task, wake)
    if decision["device"]:
        log.info("route task %s %s (%s): %s", _ref(task), task["task_id"], service, reason)
        await _forward_to_hp(task, decision["device"])
        return
    # Held until a device can take it; the supervisor asks again every few
    # seconds and fails it after router.HOLD_MAX_S.
    task["state"] = HELD
    task["worker"] = None
    task.setdefault("held_since", time.time())
    if reason != task.get("hold_reason"):
        task["hold_reason"] = reason
        log.info("route task %s %s (%s): held — %s", _ref(task), task["task_id"],
                 service, reason)
        if not wake:
            await _card_add(task, f"waiting: {reason}")


async def _wake(task: dict, device: str) -> None:
    """Boot a device, at most once per WAKE_COOLDOWN_S; one card line per
    task and device."""
    now = time.time()
    if now - _last_wake.get(device, 0.0) >= router.WAKE_COOLDOWN_S:
        _last_wake[device] = now
        _power_wake_bg(device)
    woke = task.setdefault("woke", [])
    if device not in woke:
        woke.append(device)
        await _card_add(task, f"waking device {device}")


async def _forward_to_hp(task: dict, device: str) -> None:
    if task.get("target") == "backup":
        # Belongs to the extension pool; the local server must never get it.
        task["state"] = BACKUP_AVAILABLE
        return
    worker = _workers.get(device)
    if worker is None:
        task["state"] = HELD
        task["worker"] = None
        task.setdefault("held_since", time.time())
        return
    try:
        await worker.ws.send_text(json.dumps({
            "type": "task",
            "task_id": task["task_id"],
            "service": task["service"],
            "payload": task["payload"],
            "task_type": rules_for(task).type,
            "priority": task_types.priority(task),
            "ref": task.get("ref"),
            "browsers": list(rules_for(task).browsers) or None,
        }))
        task["state"] = SENT_HP
        task["sent_at"] = time.time()
        task["worker"] = worker.id
        task["accepted"] = False
        task["started_at"] = None
        task.pop("orphan_until", None)
        task.pop("held_since", None)
        task.pop("hold_reason", None)
        _power_keepalive_bg(worker.id)
        log.info("forwarded task %s %s (service=%s) to device %s",
                 _ref(task), task["task_id"], task["service"], worker.id)
        await _card_add(task, f"sent to device {worker.id}")
    except Exception as exc:
        task["state"] = HELD
        task["worker"] = None
        task.setdefault("held_since", time.time())
        log.warning("failed to forward task %s to device %s: %s",
                    task["task_id"], worker.id, exc)


async def _cancel_on(device: str | None, task_id: str) -> None:
    """Ask a device to drop a task it still has queued (no-op if it's gone)."""
    w = _workers.get(device) if device else None
    if w is None:
        return
    try:
        await w.ws.send_text(json.dumps({"type": "cancel", "task_id": task_id}))
    except Exception:
        pass


async def _take_back(task: dict, line: str) -> None:
    """Pull a sent task off its device and route it again."""
    old = task.get("worker")
    log.warning("task %s taken back from device %s: %s", task["task_id"], old, line)
    await _card_add(task, line)
    await _cancel_on(old, task["task_id"])
    task["state"] = HELD
    task["worker"] = None
    task["accepted"] = False
    task["started_at"] = None
    task.pop("orphan_until", None)
    task["avoid"] = old
    task["avoid_until"] = time.time() + router.WAKE_TIMEOUT_S
    task["wake_tried"] = []
    _router.forget(task["task_id"])
    task.setdefault("held_since", time.time())
    await _route(task)


async def _fail_task(task: dict, status: str, error: str, line: str) -> None:
    """Finish a task with an error back to the caller."""
    if tasks.get(task["task_id"]) is not task:
        return
    task["finished_at"] = time.time()
    tasks.pop(task["task_id"], None)
    _router.forget(task["task_id"])
    _archive(task, status, error)
    await _cancel_on(task.get("worker"), task["task_id"])
    await _card_add(task, line)
    await _deliver_callback(task, status, None, error)


# ── local server websocket ────────────────────────────────────────────────────

@app.websocket("/ws/worker")
async def worker_ws(websocket: WebSocket, token: str = "", worker: str = ""):
    if token != config.WORKER_TOKEN:
        await websocket.close(code=4401, reason="bad token")
        log.warning("server connection rejected: bad token")
        return

    # Each laptop identifies itself with ?worker=<device id>. A legacy worker
    # that sends no id is treated as device "1" (backward compatible).
    worker_id = (worker or "1").strip()

    # Replace-on-reconnect PER device: a new connection for the SAME device id
    # takes over and the stale one is closed. A DIFFERENT device id just joins
    # — that is what lets both laptops work at once.
    old = _workers.get(worker_id)
    if old is not None:
        try:
            await old.ws.close(code=4409, reason="replaced by a new connection")
        except Exception:
            pass
        log.info("replacing previous connection for device %s", worker_id)

    await websocket.accept()
    w = Worker(worker_id, websocket)
    _workers[worker_id] = w
    _router.note_online(worker_id)
    log.info("device %s connected (%d online)", worker_id, len(_workers))
    await telegram_bot.notify(
        f"Local server UP: device {worker_id} ({len(_workers)} online).")

    # Tasks this device had before a short drop are still its own (the
    # worker keeps its queue); the status frame's task list confirms them.
    _power_keepalive_bg(worker_id)
    for task in tasks.values():
        if task["state"] == SENT_HP and task.get("worker") == worker_id:
            task.pop("orphan_until", None)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("device %s sent non-JSON message, ignoring", worker_id)
                continue
            mtype = message.get("type")
            if mtype == "result":
                await _handle_hp_result(message, worker_id)
            elif mtype == "status":
                _apply_status(message, w)
                await _reconcile(w)
            elif mtype == "started":
                st = tasks.get(message.get("task_id"))
                if st is not None and st.get("worker") == worker_id:
                    st["accepted"] = True
                    st["started_at"] = time.time()
            elif mtype == "cancelled":
                log.info("device %s dropped task %s (%s)", worker_id,
                         message.get("task_id"),
                         "was running" if message.get("running") else "was queued")
            elif mtype == "event":
                level = message.get("level", "info")
                text = message.get("text", "")
                log.info("device %s event [%s]: %s", worker_id, level, text)
                await telegram_bot.notify(
                    f"Local server [device {worker_id}] [{level}]: {text}")
            elif mtype == "accepted":
                # The worker queued the task, so it is alive and working.
                acc = tasks.get(message.get("task_id"))
                if acc is not None and acc.get("worker") == worker_id:
                    acc["accepted"] = True
            elif mtype == "netreport":
                _net_report_bg(message, w)
            elif mtype == "step":
                await _handle_step(message, worker_id)
            elif mtype == "resume":
                await _handle_resume(message, w)
            elif mtype == "claim":
                await _handle_claim(message, w)
    except WebSocketDisconnect:
        log.info("device %s disconnected", worker_id)
    finally:
        # Only drop it if THIS socket is still the registered one for the id
        # (a replacement may have already taken over).
        if _workers.get(worker_id) is w:
            del _workers[worker_id]
            _router.note_offline(worker_id)
            await telegram_bot.notify(
                f"Local server DOWN: device {worker_id} ({len(_workers)} online).")
            # Tasks in flight there get a short grace: a network blip keeps
            # the worker's queue, so they may still finish. The supervisor
            # takes them back if the device doesn't return in time.
            until = time.time() + router.DISCONNECT_GRACE_S
            for task in tasks.values():
                if task["state"] == SENT_HP and task.get("worker") == worker_id:
                    task["orphan_until"] = until


async def _handle_resume(message: dict, w: "Worker") -> None:
    """A restarted worker offers the tasks it saved; it keeps only those
    still assigned to it here (the rest were moved on while it was away)."""
    ids = message.get("task_ids")
    ids = ids if isinstance(ids, list) else []
    keep = []
    for tid in ids:
        task = tasks.get(tid)
        if task is not None and task["state"] == SENT_HP and task.get("worker") == w.id:
            task.pop("orphan_until", None)
            keep.append(tid)
    log.info("device %s resumed %d of %d saved tasks: %s", w.id, len(keep), len(ids),
             ", ".join(_ref(tasks[t]) for t in keep) or "-")
    try:
        await w.ws.send_text(json.dumps({"type": "resume_ok", "keep": keep}))
    except Exception:
        pass


async def _handle_claim(message: dict, w: "Worker") -> None:
    """Direct handoff: the other device took a task over the peer link. The
    gate stays the owner — it moves the task only while it's still sent to
    the offering device and hasn't started, so it never runs on two devices."""
    tid = message.get("task_id")
    frm = str(message.get("from") or "")
    task = tasks.get(tid)
    why = ""
    if task is None:
        why = "unknown task"
    elif task["state"] != SENT_HP or task.get("worker") != frm or frm == w.id:
        why = f"not on device {frm}"
    elif task.get("started_at") or _paying(task):
        why = "already started"
    elif task.get("failing"):
        why = "task is failing"
    if why:
        log.info("device %s claim of %s refused: %s", w.id, tid, why)
        reply = {"type": "claim_no", "task_id": tid, "error": why}
    else:
        await _cancel_on(frm, tid)
        await _card_add(task, f"handed from device {frm} to device {w.id}"
                              + (f" ({message.get('why')})" if message.get("why") else ""))
        task["handoffs"] = task.get("handoffs", 0) + 1
        await _forward_to_hp(task, w.id)
        ok = task.get("worker") == w.id
        reply = ({"type": "claim_ok", "task_id": tid} if ok
                 else {"type": "claim_no", "task_id": tid, "error": "forward failed"})
    try:
        await w.ws.send_text(json.dumps(reply))
    except Exception:
        pass


async def _reconcile(w: "Worker") -> None:
    """A task the gate thinks is on this device but the worker doesn't have
    (worker restarted, lost it) goes back to routing right away."""
    if w.task_ids is None:
        return
    now = time.time()
    for task in list(tasks.values()):
        if (task["state"] == SENT_HP and task.get("worker") == w.id
                and task["task_id"] not in w.task_ids
                and now - (task.get("sent_at") or now) > 15
                and not task.get("failing")):
            if _paying(task):
                _fail_paying(task, w.id, f"device {w.id} lost the payment (worker restarted)")
            else:
                await _take_back(task, f"device {w.id} no longer has it — re-routing")


def _apply_status(message: dict, worker: "Worker") -> None:
    worker.last_status = time.time()
    loop_age = message.get("loop_age")
    worker.loop_age = float(loop_age) if isinstance(loop_age, (int, float)) else None
    ids = message.get("task_ids")
    worker.task_ids = set(ids) if isinstance(ids, list) else None
    if isinstance(message.get("version"), str):
        worker.version = message["version"][:16]
    link = message.get("link")
    if isinstance(link, dict):
        worker.link = {"link": str(link.get("link") or "")[:8],
                       "host": str(link.get("host") or "")[:64],
                       "on_ip_s": int(link.get("on_ip_s") or 0)}
    browsers = message.get("browsers")
    if isinstance(browsers, dict):
        snap: dict = {}
        for name, info in browsers.items():
            if isinstance(info, dict):
                snap[name] = {
                    "running": bool(info.get("running")),
                    "ext_connected": bool(info.get("ext_connected")),
                    "down_s": int(info.get("down_s") or 0),
                }
        worker.browsers = snap


def _archive(task: dict, status: str, error=None) -> None:
    fin = task.get("finished_at") or time.time()
    try:
        stats.record(rules_for(task).type, task.get("worker"), status,
                     fin - task["created"] if task.get("created") else None, fin)
    except Exception as exc:                          # noqa: BLE001
        log.warning("stats record failed: %s", exc)
    history.appendleft({
        "task_id": task["task_id"],
        "service": task["service"],
        "type": rules_for(task).type,
        "num": task.get("num"),
        "ref": task.get("ref"),
        "steps": task.get("steps") or [],
        "cleanup_needed": task.get("cleanup_needed") or [],
        "status": status,
        "error": error,
        "worker": task.get("worker"),
        "attempts": task.get("attempts", 0),
        "created": task.get("created"),
        "finished_at": task.get("finished_at") or time.time(),
    })


async def _handle_hp_result(message: dict, from_device: str) -> None:
    task_id = message.get("task_id")
    task = tasks.get(task_id)
    if task is None:
        log.warning("server result for unknown task %s, dropping", task_id)
        return
    status = message.get("status", "error")
    owner = task.get("worker")
    if from_device != owner and status != "done":
        # The task was moved to another device meanwhile; only a finished
        # result from the old one is still worth taking.
        log.info("ignoring %s for task %s from device %s (now on %s)",
                 status, task_id, from_device, owner)
        return
    if from_device != owner:
        await _cancel_on(owner, task_id)
    device = from_device
    task["worker"] = device
    browser = message.get("browser") or ""
    where = f"device {device}" + (f" ({browser})" if browser else "")
    _power_keepalive_bg(device)   # a result is activity — keep that PC's timer fresh

    # "retry" = a rejected login, or a batch_job task every browser on that PC
    # failed without paying: route the SAME task again (the router prefers the
    # other device, waking it if needed) until the budget is spent.
    rules = rules_for(task)
    if status == "retry" and router.can_bounce(task):
        if _router.bounce(task):
            if rules.retry == "other_pc_once":
                line = (f"all browsers failed on device {device} "
                        f"({message.get('error') or 'error'}) — trying the other PC")
            else:
                line = (f"rejected on {where} — re-routing "
                        f"(attempt {task.get('attempts')})")
            await _card_add(task, line)
            task["state"] = HELD
            task["worker"] = None
            task["accepted"] = False
            task["started_at"] = None
            task["held_since"] = time.time()
            task.pop("hold_reason", None)
            task.pop("woke", None)
            await _route(task)
            return
        if rules.type == "login":
            message["error"] = message.get("error") or "cannot login"
        else:
            message["error"] = message.get("error") or "failed on both PCs"
        status = "error"
        log.info("task %s bounce budget spent -> %s", task_id, message["error"])

    if status == "done":
        task["finished_at"] = time.time()
        took = task["finished_at"] - task.get("created", task["finished_at"])
        await _card_add(task, f"DONE on {where} — {int(took // 60)}m{int(took % 60)}s")
        cleanup = _cleanup_failures(task, message.get("result"))
        if cleanup:
            task["cleanup_needed"] = cleanup
            await _card_add(task, "manual cleanup needed: " + "; ".join(cleanup))
            _cleanup_alert(task, cleanup)
        await _deliver_callback(task, "done", message.get("result"), None)
        _router.forget(task_id)
        _archive(task, "done")
        tasks.pop(task_id, None)
        return

    # error / timeout -> the CALLER owns the failure flow (the
    # caller decides what happens next). Deliver the error straight back to the callback, same
    # as "done" — do NOT park the task in the backup extension pool.
    task["finished_at"] = time.time()
    if str(message.get("error") or "").startswith("payment_unconfirmed"):
        _money_alert(task, f"payment unconfirmed on {where}: {message.get('error')}")
    await _card_add(task, f"FAILED on {where} [{status}] "
                          f"{message.get('error') or ''}".rstrip())
    await _deliver_callback(task, status, message.get("result"),
                            message.get("error"))
    _router.forget(task_id)
    _archive(task, status, message.get("error"))
    tasks.pop(task_id, None)
    log.info("server failed task %s (%s) -> error sent to caller", task_id, status)


# ── backup extension endpoints (password auth via X-Ext-Auth) ─────────────────

def _check_ext_auth(x_ext_auth: str | None) -> None:
    if x_ext_auth != config.EXT_AUTH:
        raise HTTPException(status_code=401, detail="bad ext auth")


class ExtTake(BaseModel):
    task_id: str


class ExtResult(BaseModel):
    task_id: str
    status: str = "done"
    result: dict | None = None
    error: str | None = None


@app.get("/ext/poll")
async def ext_poll(x_ext_auth: str | None = Header(default=None)):
    _check_ext_auth(x_ext_auth)
    for task in tasks.values():
        if task["state"] == BACKUP_AVAILABLE:
            return {
                "task_id": task["task_id"],
                "service": task["service"],
                "payload": task["payload"],
            }
    return {}


@app.post("/ext/take")
async def ext_take(body: ExtTake, x_ext_auth: str | None = Header(default=None)):
    _check_ext_auth(x_ext_auth)
    task = tasks.get(body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    if task["state"] != BACKUP_AVAILABLE:
        return {"ok": False, "reason": f"task is {task['state']}"}
    task["state"] = BACKUP_CLAIMED
    task["claimed_at"] = time.time()
    log.info("backup task %s claimed by extension", body.task_id)
    return {"ok": True}


@app.post("/ext/result")
async def ext_result(body: ExtResult, x_ext_auth: str | None = Header(default=None)):
    _check_ext_auth(x_ext_auth)
    task = tasks.get(body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    task["finished_at"] = time.time()
    await _deliver_callback(task, body.status, body.result, body.error)
    _archive(task, body.status, body.error)
    tasks.pop(body.task_id, None)
    return {"ok": True}


# ── admin API (loopback; X-Admin-Key) ─────────────────────────────────────────

def _check_admin(x_admin_key: str | None) -> None:
    if x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=401, detail="bad admin key")


# ── local-server device feed ──────────────────────────────────────────────────
# The local-server gate (:8201) pushes over loopback which device is working
# (device 1 or device 2) whenever a device's state changes and
# every ~60s as a refresh.

_localserver_devices: dict = {"devices": [], "updated_at": 0.0}


@app.post("/localserver/devices")
async def localserver_devices_push(
        body: dict, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    devices = body.get("devices")
    if not isinstance(devices, list):
        raise HTTPException(status_code=400, detail="devices list required")
    _localserver_devices["devices"] = devices
    _localserver_devices["updated_at"] = time.time()
    return {"ok": True, "count": len(devices)}


@app.get("/localserver/devices")
async def localserver_devices_get(
        x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return {"ok": True, **_localserver_devices}


def _task_public(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "service": task["service"],
        "type": rules_for(task).type,
        "num": task.get("num"),
        "ref": task.get("ref"),
        "state": task["state"],
        "target": task.get("target", "hp"),
        "worker": task.get("worker"),
        "created": task.get("created"),
        "sent_at": task.get("sent_at"),
        "accepted": task.get("accepted"),
        "started_at": task.get("started_at"),
        "held_since": task.get("held_since"),
        "hold_reason": task.get("hold_reason"),
        "attempts": task.get("attempts", 0),
        "error": (task.get("hp_error") or {}).get("error"),
        "last_step": task.get("last_step"),
        "paid_step": task.get("paid_step"),
    }


@app.get("/admin/status")
async def admin_status(x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    counts: dict[str, int] = {}
    for t in tasks.values():
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    now = time.time()
    workers = [{
        "id": w.id,
        "connected": True,
        "uptime": now - w.connected_at,
        "inflight": _inflight(w.id),
        "healthy": _worker_healthy(w),
        "status_age": now - w.last_status if w.last_status else None,
        "loop_age": w.loop_age,
        "version": w.version,
        "link": w.link,
        "browsers": w.browsers,
    } for w in sorted(_workers.values(), key=lambda x: x.id)]
    oldest = min((w.connected_at for w in _workers.values()), default=None)
    return {
        # backward-compatible fields for the admin status page
        "worker_connected": _worker_connected(),
        "worker_uptime": (now - oldest) if oldest else 0,
        "browsers": _merged_browsers(),
        # multi-device view
        "worker_count": len(_workers),
        "workers": workers,
        "gate_uptime": now - _gate_started,
        "counts": counts,
        "router": _router.snapshot(),
        "load": {s: _load(s) for s in (router.LOGIN_SERVICE, router.BATCH_JOB)},
        "load_by_type": _load_by_type(),
        "types": task_types.table(),
        "ready": {s: sorted(_ready_ids(s)) for s in
                  (router.LOGIN_SERVICE, router.BATCH_JOB, "form_submit")},
        "localserver_devices": _localserver_devices,
    }


@app.get("/admin/tasks")
async def admin_tasks(ref: str | None = None,
                      x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    live = [_task_public(t) for t in tasks.values()]
    hist = list(history)
    if ref:
        want = _norm_ref(ref)
        live = [t for t in live if _norm_ref(t.get("ref")) == want]
        hist = [t for t in hist if _norm_ref(t.get("ref")) == want]
    return {"live": live, "history": hist}


@app.get("/admin/stats")
async def admin_stats(days: int = 7, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return {"ok": True, "days": stats.days(max(1, min(int(days), stats.KEEP_DAYS))),
            "rotation": await _rotation_counts()}


def _norm_ref(ref) -> str:
    """'29/12-5', ' 29/12-05 ', '29-12-5' all mean the same task today."""
    text = str(ref or "").strip().lstrip("#").replace("-", "/")
    parts = [p for p in text.split("/") if p]
    try:
        return "/".join(str(int(p)) for p in parts)
    except ValueError:
        return text


def _find_ref(ref: str) -> str:
    """Telegram /task <ref>: the task's card-style summary."""
    want = _norm_ref(ref)
    for t in tasks.values():
        if _norm_ref(t.get("ref")) == want:
            return (_card_text(t) + f"\nstate: {t['state']}"
                    + (f" on device {t['worker']}" if t.get("worker") else ""))
    for row in history:
        if _norm_ref(row.get("ref")) == want:
            took = (row.get("finished_at") or 0) - (row.get("created") or 0)
            return (f"{row['ref']}  {row['service']} | {row.get('type')}\n"
                    f"{row['status']} on device {row.get('worker') or '?'}"
                    f" after {int(took // 60)}m{int(took % 60)}s"
                    + (f"\nerror: {row['error']}" if row.get("error") else "")
                    + (f"\ncleanup needed: {'; '.join(row['cleanup_needed'])}"
                       if row.get("cleanup_needed") and not row.get("cleanup_fixed") else ""))
    return f"No task {ref} in the live list or the recent history."


@app.post("/admin/history/{task_id}/cleanup_fixed")
async def admin_cleanup_fixed(task_id: str, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    for row in history:
        if row["task_id"] == task_id and row.get("cleanup_needed"):
            row["cleanup_fixed"] = time.time()
            return {"ok": True}
    raise HTTPException(status_code=404, detail="no cleanup pending for that task")


@app.get("/admin/outbox")
async def admin_outbox(x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    now = time.time()
    return {"items": [
        {k: item.get(k) for k in ("task_id", "ref", "service", "status", "url",
                                  "attempts", "last_error")}
        | {"age_s": round(now - item["first_at"]),
           "next_in_s": max(0, round(float(item["next_at"]) - now))}
        for item in _outbox.values()]}


@app.post("/admin/outbox/{task_id}/retry")
async def admin_outbox_retry(task_id: str, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    item = _outbox.get(task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not in the outbox")
    item["next_at"] = 0
    await _outbox_pass()
    return {"ok": True, "delivered": task_id not in _outbox}


class AdminTest(BaseModel):
    service: str = Field(default="echo", pattern="^(echo|ext_echo)$")
    payload: dict = Field(default_factory=dict)


@app.post("/admin/test")
async def admin_test(body: AdminTest, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    # Test task; its result lands in history via the internal sink.
    # "echo" tests gate <-> local server; "ext_echo" goes on through a browser
    # extension and back.
    port = config.GATEWAY_PORT
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "service": body.service,
        "callback_url": f"http://127.0.0.1:{port}/admin/sink",
        "payload": body.payload or {"ping": "admin-test", "ts": time.time()},
        "state": HELD,
        "hp_error": None,
        "worker": None,
        "created": time.time(),
        "sent_at": None,
        "finished_at": None,
        "claimed_at": None,
    }
    task["ref"] = _new_ref()
    _save_state()
    tasks[task_id] = task
    await _route(task)
    if task["state"] == SENT_HP:
        return {"task_id": task_id, "ref": task["ref"], "status": "sent",
                "worker": task.get("worker")}
    return {"task_id": task_id, "ref": task["ref"], "status": "held",
            "note": task.get("hold_reason", "")}


@app.post("/admin/sink")
async def admin_sink(body: dict):
    # Internal callback target for admin test tasks. Nothing to do; the task is
    # already archived to history by _handle_hp_result.
    return {"ok": True}


@app.post("/admin/tasks/{task_id}/retry")
async def admin_retry(task_id: str, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    # A backup task belongs to the extension pool only — retry re-arms it
    # there instead of handing it to the local server, which never sees
    # target=backup work.
    if task.get("target") == "backup":
        task["state"] = BACKUP_AVAILABLE
        task["claimed_at"] = None
        return {"ok": True, "task_id": task_id, "state": BACKUP_AVAILABLE}
    if not _worker_connected():
        raise HTTPException(status_code=409, detail="server offline")
    if task["state"] == SENT_HP:
        await _cancel_on(task.get("worker"), task_id)
    task["state"] = HELD
    task["hp_error"] = None
    task["worker"] = None   # let the router re-pick a device
    task["held_since"] = time.time()
    _router.forget(task_id)
    await _route(task)
    return {"ok": True, "task_id": task_id, "state": task["state"]}


@app.post("/admin/tasks/{task_id}/cancel")
async def admin_cancel(task_id: str, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    task["finished_at"] = time.time()
    tasks.pop(task_id, None)
    _router.forget(task_id)
    await _cancel_on(task.get("worker"), task_id)
    await _deliver_callback(task, "cancelled", None, "cancelled by admin")
    _archive(task, "cancelled", "cancelled by admin")
    return {"ok": True, "task_id": task_id}


class AdminBrowser(BaseModel):
    browser: str = Field(min_length=1)
    action: str = Field(pattern="^(start|close|down|up)$")
    # Optional device id. Omitted = apply to every connected device.
    worker: str | None = None
    # "down" only: seconds the browser takes no task (drills)
    for_s: float | None = None


@app.post("/admin/browser")
async def admin_browser(body: AdminBrowser, x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    if not _worker_connected():
        raise HTTPException(status_code=409, detail="server offline")
    if body.worker:
        target = _workers.get(body.worker)
        if target is None:
            raise HTTPException(status_code=409, detail=f"device {body.worker} offline")
        targets = [target]
    else:
        targets = list(_workers.values())
    frame = json.dumps({
        "type": "browser",
        "action": body.action,
        "browser": body.browser,
        "for_s": body.for_s,
    })
    for w in targets:
        await w.ws.send_text(frame)
    return {"ok": True, "browser": body.browser, "action": body.action,
            "workers": [w.id for w in targets]}


@app.get("/admin/tokens")
async def admin_tokens(x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return {
        "worker_token": config.WORKER_TOKEN,
        "ext_auth": config.EXT_AUTH,
        "admin_key": config.ADMIN_KEY,
    }


# ── callback delivery ─────────────────────────────────────────────────────────

async def _deliver_callback(task: dict, status: str, result, error) -> None:
    body = {
        "task_id": task["task_id"],
        "ref": task.get("ref"),
        "service": task["service"],
        "status": status,
        "result": result,
        "error": error,
    }
    url = task["callback_url"]
    err = ""
    for attempt in range(1, config.CALLBACK_RETRIES + 1):
        err = await _post_callback(url, body, task["task_id"])
        if not err:
            return
        log.warning("callback attempt %d/%d for task %s failed: %s",
                    attempt, config.CALLBACK_RETRIES, task["task_id"], err)
        if attempt < config.CALLBACK_RETRIES:
            await asyncio.sleep(config.CALLBACK_RETRY_DELAY * attempt)
    _outbox_add(task, url, body, err)


async def _post_callback(url: str, body: dict, task_id: str) -> str:
    """One delivery try. Returns "" when the caller took it, else the error.
    A 5xx means the caller couldn't store it, so it counts as not delivered."""
    try:
        resp = await _http.post(url, json=body)
    except Exception as exc:
        return str(exc) or exc.__class__.__name__
    log.info("callback for task %s -> %s (HTTP %d)", task_id, url, resp.status_code)
    if resp.status_code >= 500:
        return f"HTTP {resp.status_code}"
    return ""


# ── results outbox: a finished result is never dropped ────────────────────────

OUTBOX_MAX_AGE_S = float(os.getenv("OUTBOX_MAX_AGE_S", str(24 * 3600)))
OUTBOX_MAX_GAP_S = float(os.getenv("OUTBOX_MAX_GAP_S", "300"))
_OUTBOX_FILE = os.getenv("GATE_OUTBOX_FILE",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "outbox.json"))
_outbox: dict[str, dict] = {}


def _outbox_gap(attempts: int) -> float:
    return min(OUTBOX_MAX_GAP_S, 10.0 * (2 ** min(attempts, 10)))


def _outbox_add(task: dict, url: str, body: dict, err: str) -> None:
    now = time.time()
    _outbox[task["task_id"]] = {
        "task_id": task["task_id"], "ref": task.get("ref"), "service": task["service"],
        "status": body.get("status"), "url": url, "body": body,
        "first_at": now, "attempts": 0, "next_at": now + _outbox_gap(0),
        "last_error": err,
    }
    _outbox_save()
    log.error("callback for task %s undelivered, kept in the outbox: %s",
              task["task_id"], err)
    telegram_bot.notify_bg(
        f"Result not delivered: task {_ref(task)} {task['service']} "
        f"({body.get('status')}) to {url}: {err}. Kept in the outbox, "
        f"retrying for {int(OUTBOX_MAX_AGE_S // 3600)} h.")


def _outbox_save() -> None:
    if _FERNET is None:
        log.warning("TASKS_KEY not set: outbox is not saved to disk")
        return
    try:
        data = json.dumps(list(_outbox.values()), default=str).encode("utf-8")
        _write_private(_OUTBOX_FILE, _FERNET.encrypt(data))
    except OSError as exc:
        log.warning("could not save the outbox: %s", exc)


def _outbox_load() -> int:
    if _FERNET is None or not os.path.exists(_OUTBOX_FILE):
        return 0
    try:
        with open(_OUTBOX_FILE, "rb") as fh:
            saved = json.loads(_FERNET.decrypt(fh.read()))
    except (OSError, ValueError, InvalidToken) as exc:
        log.warning("could not load the outbox: %s", exc)
        return 0
    for item in saved if isinstance(saved, list) else []:
        if isinstance(item, dict) and item.get("task_id"):
            item["next_at"] = time.time()
            _outbox[item["task_id"]] = item
    return len(_outbox)


async def _outbox_pass() -> None:
    now = time.time()
    changed = False
    for tid, item in list(_outbox.items()):
        if now < float(item.get("next_at") or 0):
            continue
        err = await _post_callback(item["url"], item["body"], tid)
        if not err:
            _outbox.pop(tid, None)
            changed = True
            age = int((time.time() - item["first_at"]) // 60)
            log.info("outbox: task %s delivered after %d min", tid, age)
            telegram_bot.notify_bg(
                f"Result delivered late: {item['service']} task {tid} "
                f"(after {age} min, {item['attempts'] + 1} tries).")
            continue
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_error"] = err
        item["next_at"] = time.time() + _outbox_gap(item["attempts"])
        changed = True
        if time.time() - item["first_at"] >= OUTBOX_MAX_AGE_S:
            _outbox.pop(tid, None)
            log.error("outbox: gave up on task %s after %d tries", tid, item["attempts"])
            telegram_bot.notify_bg(
                f"GAVE UP delivering {item['service']} ({item.get('status')}) "
                f"task {tid} to {item['url']} after "
                f"{int(OUTBOX_MAX_AGE_S // 3600)} h: {err}. Deliver it by hand.")
    if changed:
        _outbox_save()


async def _outbox_loop() -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await _outbox_pass()
        except Exception:
            log.exception("outbox pass failed")


# ── TTL sweeper: give up on backup tasks the extension never finishes ─────────

def _who(task: dict) -> str:
    p = task.get("payload") or {}
    who = (p.get("username") or p.get("nickname") or p.get("email")
           or p.get("account_email") or "?")
    return f"{'user' if p.get('username') or p.get('nickname') else 'account'} {who}"


def _money_alert(task: dict, what: str) -> None:
    p = task.get("payload") or {}
    action = f" {p['action']}" if p.get("action") else ""
    telegram_bot.notify_bg(
        f"WARNING (check manually): task {_ref(task)} "
        f"{task['service']}{action} {_who(task)} "
        f"job {p.get('job_id') or '?'} — {what}")


def _paying(task: dict) -> bool:
    """A task of a type that must never re-run once started (money may be
    moving): never re-route it. The worker's step frames can't say how far
    the extension got inside one browser run, so started counts."""
    return (not rules_for(task).rerun_after_start) and bool(task.get("started_at"))


def _cleanup_failures(task: dict, result) -> list[str]:
    """Cleanup steps the worker ran after the task (they don't fail it).
    The result carries {"cleanup": {"<step>": {"ok": bool}}}; returns the failed ones."""
    if not isinstance(result, dict) or not isinstance(result.get("cleanup"), dict):
        return []
    return [f"{name} failed" for name, step in result["cleanup"].items()
            if isinstance(step, dict) and step.get("ok") is False]


def _cleanup_alert(task: dict, what: list[str]) -> None:
    p = task.get("payload") or {}
    telegram_bot.notify_bg(
        f"manual cleanup needed: task {_ref(task)} {task['service']} "
        f"{_who(task)} job {p.get('job_id') or '?'} — paid, but "
        + "; ".join(what))


async def _handle_step(message: dict, from_device: str) -> None:
    """One finished step of a multi-step flow: {name, ok, note, paid}."""
    task = tasks.get(message.get("task_id"))
    if task is None or task.get("worker") != from_device:
        return
    name = str(message.get("name") or "?")[:40]
    ok = bool(message.get("ok", True))
    note = str(message.get("note") or "")[:200]
    step = {"name": name, "ok": ok, "note": note, "at": time.time()}
    if message.get("paid"):
        step["paid"] = True
        task["paid_step"] = name
    task.setdefault("steps", []).append(step)
    task["last_step"] = name
    await _card_add(task, f"step {name}: {'ok' if ok else 'FAILED'}"
                          + (f" — {note}" if note else ""))
    _persist_tasks()


def _fail_paying(task: dict, device, why: str) -> None:
    _money_alert(task, why)
    task["failing"] = True
    asyncio.create_task(_fail_task(
        task, "error", "payment state unknown — check manually",
        f"FAILED — {why}; payment state unknown, check manually"))


async def _check_sent(task: dict, now: float) -> None:
    """Timers for a task a device holds: disconnect grace, acknowledgement,
    time queued, time running."""
    device = task.get("worker")
    w = _workers.get(device) if device else None
    sent_ago = now - (task.get("sent_at") or now)
    if w is None:
        until = task.setdefault("orphan_until", now + router.DISCONNECT_GRACE_S)
        if now >= until:
            if _paying(task):
                _fail_paying(task, device, f"device {device} disconnected mid-payment")
            else:
                await _take_back(task, f"device {device} disconnected — re-routing")
        return
    if not task.get("accepted"):
        if sent_ago >= router.ACK_TIMEOUT_S:
            await _take_back(task, f"device {device} didn't respond — re-routing")
        return
    started = task.get("started_at")
    limit = router.run_limit(task)
    if started:
        if now - started < limit:
            return
    elif w.task_ids is not None:
        # a worker that reports "started": queued too long, or its loop froze
        stalled = w.loop_age is not None and w.loop_age >= router.LOOP_STALE_S
        if sent_ago < router.QUEUE_MAX_S and not stalled:
            return
        await _take_back(task, f"device {device} didn't start it — re-routing")
        return
    elif sent_ago < router.QUEUE_MAX_S + limit:
        return                     # older worker: one overall limit
    if _paying(task):
        _fail_paying(task, device, f"no result from device {device} in time")
        return
    if not task.get("rerun"):
        task["rerun"] = True
        await _take_back(task, f"no result from device {device} in time — re-routing once")
        return
    task["failing"] = True
    asyncio.create_task(_fail_task(
        task, "timeout", "no result from the local server",
        f"FAILED — no result from device {device} in time"))


async def _supervisor() -> None:
    """Every few seconds: re-check held tasks (a woken device came up, a
    slot freed, a wake timed out), watch sent tasks, note main swaps, save
    live tasks."""
    global _last_main
    while True:
        await asyncio.sleep(5)
        try:
            now = time.time()
            main = router.main_device(now)
            if _last_main is not None and main != _last_main:
                log.info("main device is now %s", main)
                telegram_bot.notify_bg(
                    f"Main device is now device {main} (until "
                    f"{_local(router.main_until(now), '%H:%M')} local time).")
            _last_main = main
            for task in list(tasks.values()):
                if task.get("failing") or task.get("target") == "backup":
                    continue
                if task["state"] == SENT_HP:
                    await _check_sent(task, now)
                elif task["state"] == HELD:
                    held = now - float(task.get("held_since") or now)
                    if held >= router.HOLD_MAX_S:
                        task["failing"] = True
                        telegram_bot.notify_bg(
                            f"WARNING: task {_ref(task)} "
                            f"{task['service']} failed — no device available "
                            f"for {int(held // 60)} min.")
                        asyncio.create_task(_fail_task(
                            task, "error", "no device available",
                            "FAILED — no device available"))
            await _route_held()
            _persist_tasks()
            _check_versions(now)
        except Exception:
            log.exception("supervisor pass failed")


_versions_differ_since: float | None = None
_versions_alerted = False
VERSION_GRACE_S = 600.0


def _check_versions(now: float) -> None:
    """One alert when the connected workers run different code for longer
    than an update takes."""
    global _versions_differ_since, _versions_alerted
    vs = {w.id: w.version for w in _workers.values() if w.version}
    if len(set(vs.values())) <= 1:
        if _versions_alerted and len(vs) > 1:
            telegram_bot.notify_bg(f"Worker versions match again ({next(iter(vs.values()))}).")
        _versions_differ_since, _versions_alerted = None, False
        return
    if _versions_differ_since is None:
        _versions_differ_since = now
    if not _versions_alerted and now - _versions_differ_since >= VERSION_GRACE_S:
        _versions_alerted = True
        telegram_bot.notify_bg("WARNING: worker versions differ: " + ", ".join(
            f"device {d} {v}" for d, v in sorted(vs.items()))
            + ". Run deploy\\cluster_update.py.")


# ── live task persistence (a gate restart must not lose tasks) ────────────────

_TASKS_FILE = os.getenv("GATE_TASKS_FILE",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "tasks_live.json"))
_last_persisted: str = ""

# Live tasks can carry credentials: the file is encrypted with
# TASKS_KEY (a Fernet key in .env) and readable by root only.
try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET = Fernet(os.environ["TASKS_KEY"]) if os.getenv("TASKS_KEY") else None
except (ImportError, ValueError) as _exc:
    log.warning("tasks file encryption unavailable: %s", _exc)
    _FERNET, InvalidToken = None, ValueError


def _write_private(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _persist_tasks() -> None:
    global _last_persisted
    try:
        blob = json.dumps(list(tasks.values()), default=str)
    except (TypeError, ValueError) as exc:
        log.warning("could not serialise live tasks: %s", exc)
        return
    if blob == _last_persisted:
        return
    if _FERNET is None:
        log.warning("TASKS_KEY not set: live tasks are not saved to disk")
        _last_persisted = blob
        return
    try:
        _write_private(_TASKS_FILE, _FERNET.encrypt(blob.encode("utf-8")))
        _last_persisted = blob
    except OSError as exc:
        log.warning("could not save live tasks: %s", exc)


def _load_saved_tasks():
    with open(_TASKS_FILE, "rb") as fh:
        raw = fh.read()
    if raw.lstrip().startswith(b"["):          # written before encryption
        return json.loads(raw)
    if _FERNET is None:
        raise ValueError("tasks file is encrypted but TASKS_KEY is not set")
    return json.loads(_FERNET.decrypt(raw))


def _restore_tasks() -> int:
    """Load the tasks live at the last shutdown. Held ones are routed by the
    supervisor; sent ones wait for their device, with fresh timers."""
    try:
        saved = _load_saved_tasks()
    except (OSError, ValueError, InvalidToken) as exc:
        if not isinstance(exc, FileNotFoundError):
            log.warning("could not restore live tasks: %s", exc)
        return 0
    now = time.time()
    n = 0
    for task in saved if isinstance(saved, list) else []:
        if not isinstance(task, dict) or not task.get("task_id"):
            continue
        task.pop("failing", None)
        if task.get("state") == SENT_HP:
            task["sent_at"] = now
            if task.get("started_at"):
                task["started_at"] = now
            # workers reconnect a few seconds after a gate restart
            task["orphan_until"] = now + 60
        elif task.get("state") == HELD:
            task["held_since"] = now
        tasks[task["task_id"]] = task
        n += 1
    if n:
        log.info("restored %d live task(s) from %s", n, _TASKS_FILE)
    return n


async def _ttl_sweeper() -> None:
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for task_id, task in list(tasks.items()):
            if task["state"] not in (BACKUP_AVAILABLE, BACKUP_CLAIMED):
                continue
            if now - task["created"] < config.EXT_TASK_TTL:
                continue
            hp_error = task.get("hp_error") or {"status": "error", "error": "no result"}
            log.warning("backup TTL expired for task %s, delivering error", task_id)
            task["finished_at"] = time.time()
            await _deliver_callback(task, hp_error.get("status", "error"),
                                    hp_error.get("result"), hp_error.get("error"))
            _archive(task, hp_error.get("status", "error"), hp_error.get("error"))
            tasks.pop(task_id, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.GATEWAY_HOST, port=config.GATEWAY_PORT)
