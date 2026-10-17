"""Local integration run of the gate against fake workers (no Telegram, no
power gate). Run: python _itest_gate.py"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "POWER_GATE_URL": "",
    "WORKER_TOKEN": "t", "ADMIN_KEY": "k", "GATEWAY_PORT": "18102",
    "DISCONNECT_GRACE_S": "3", "QUEUE_MAX_S": "8", "WAKE_TIMEOUT_S": "6",
    "HOLD_MAX_S": "40", "ACK_TIMEOUT_S": "4", "CALLBACK_RETRIES": "1",
    "GATE_TASKS_FILE": os.path.join(tempfile.gettempdir(), "itest_tasks.json"),
    "GATE_STATE_FILE": os.path.join(tempfile.gettempdir(), "itest_state.json"),
    "GATE_STATS_FILE": os.path.join(tempfile.gettempdir(), "itest_stats.json"),
    "TASKS_KEY": "yZ0zX3TqE2C1bq3n0nY1bR1cC6o0m7H2d4u3pQ9aX1s=",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx          # noqa: E402
import uvicorn        # noqa: E402
import websockets     # noqa: E402

import main as gate   # noqa: E402
import router         # noqa: E402

BASE = "http://127.0.0.1:18102"
H = {"X-Admin-Key": "k"}
fails = []


def check(name, got, want):
    print(("ok  " if got == want else "FAIL") + f" {name}: {got}" + ("" if got == want else f" (want {want})"))
    if got != want:
        fails.append(name)


class FakeWorker:
    def __init__(self, wid, ext=("chrome",), start=True, new_style=True, resume=None):
        self.wid, self.ext, self.start, self.new = wid, ext, start, new_style
        self.resume = resume
        self.kept = None
        self.claims: list[tuple] = []
        self.got: list[str] = []
        self.ws = None
        self.task = None

    async def status(self):
        msg = {"type": "status",
               "browsers": {b: {"running": True, "ext_connected": b in self.ext}
                            for b in ("chrome", "brave", "vivaldi")}}
        if self.new:
            msg["loop_age"] = 0.5
            msg["task_ids"] = list(self.got)
        await self.ws.send(json.dumps(msg))

    async def run(self):
        url = f"ws://127.0.0.1:18102/ws/worker?token=t&worker={self.wid}"
        async with websockets.connect(url) as ws:
            self.ws = ws
            if self.resume:
                await ws.send(json.dumps({"type": "resume", "task_ids": self.resume}))
                self.got.extend(self.resume)
            await self.status()
            async def pinger():
                while True:
                    await asyncio.sleep(1)
                    await self.status()
            p = asyncio.create_task(pinger())
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m["type"] == "task":
                        self.got.append(m["task_id"])
                        await ws.send(json.dumps({"type": "accepted", "task_id": m["task_id"]}))
                        if self.start:
                            await ws.send(json.dumps({"type": "started", "task_id": m["task_id"]}))
                    elif m["type"] == "resume_ok":
                        self.kept = m["keep"]
                        self.got = [t for t in self.got
                                    if t in self.kept or t not in (self.resume or [])]
                    elif m["type"] == "cancel":
                        if m["task_id"] in self.got:
                            self.got.remove(m["task_id"])
                    elif m["type"] in ("claim_ok", "claim_no"):
                        self.claims.append((m["type"], m.get("error")))
            finally:
                p.cancel()

    async def finish(self, tid, status="done", error=None):
        self.got.remove(tid)
        await self.ws.send(json.dumps({"type": "result", "task_id": tid,
                                       "status": status, "result": {}, "error": error,
                                       "browser": "chrome"}))

    async def claim(self, tid, frm):
        await self.ws.send(json.dumps({"type": "claim", "task_id": tid, "from": frm,
                                       "why": "test"}))

    def up(self):
        self.task = asyncio.create_task(self.run())

    async def down(self):
        self.task.cancel()
        await asyncio.sleep(0.3)


async def live(c):
    return (await c.get(f"{BASE}/admin/tasks", headers=H)).json()["live"]


async def submit(c, service, payload=None):
    r = await c.post(f"{BASE}/task", json={"service": service,
                                           "callback_url": f"{BASE}/admin/sink",
                                           "payload": payload or {}})
    return r.json()["task_id"]


def by_worker(rows, service=None):
    out = {}
    for t in rows:
        if service and t["service"] != service:
            continue
        key = t["worker"] if t["state"] == "sent_hp" else t["state"]
        out[key] = out.get(key, 0) + 1
    return out


async def scenario():
    main_dev = router.main_device()
    other = "2" if main_dev == "1" else "1"
    print("main device now:", main_dev)
    w = {main_dev: FakeWorker(main_dev), other: FakeWorker(other)}
    for fw in w.values():
        fw.up()
    await asyncio.sleep(1.5)
    async with httpx.AsyncClient(timeout=10) as c:
        # 1. 11 logins: 5 main, 5 other, 1 held
        ids = [await submit(c, "web_login", {"email": f"a{i}@x"}) for i in range(11)]
        rows = await live(c)
        check("login fill", by_worker(rows), {main_dev: 5, other: 5, "held": 1})

        # 2. main finishes one -> the held one goes to main within a pass
        await w[main_dev].finish(w[main_dev].got[0])
        await asyncio.sleep(6)
        check("held login takes the freed slot", by_worker(await live(c)), {main_dev: 5, other: 5})

        # 3. batch_job commit: 3 on main, rest to other (separate cap)
        for i in range(4):
            await submit(c, "batch_job", {"action": "commit"})
        check("batch_job cap 3 then other",
              by_worker(await live(c), "batch_job"), {main_dev: 3, other: 1})

        # 4. code with no Brave extension connected anywhere: sent right away
        #    (the worker opens Brave itself), to the less busy device
        cid = await submit(c, "form_submit")
        row = [t for t in await live(c) if t["task_id"] == cid][0]
        check("code sent without a brave extension", (row["state"], row["worker"]),
              ("sent_hp", other))

        # 5. rejected login bounces to the other device
        bt = w[main_dev].got[0]
        await w[main_dev].finish(bt, "retry")
        row = [t for t in await live(c) if t["task_id"] == bt][0]
        check("bounced login held (other full)", row["state"], "held")
        await w[other].finish([t for t in w[other].got
                               if t in ids][0])
        await asyncio.sleep(6)
        row = [t for t in await live(c) if t["task_id"] == bt][0]
        check("bounced login -> other device", (row["state"], row["worker"]), ("sent_hp", other))

        # 6. main disconnects: after the grace its tasks move to routing
        n_main = by_worker(await live(c)).get(main_dev, 0)
        await w[main_dev].down()
        await asyncio.sleep(2)
        check("within grace still on main", by_worker(await live(c)).get(main_dev, 0), n_main)
        await asyncio.sleep(8)
        rows = await live(c)
        check("after grace nothing on main", by_worker(rows).get(main_dev, 0), 0)

        # 7. a device that accepts but never starts: taken back after QUEUE_MAX_S
        w[main_dev] = FakeWorker(main_dev, ext=("chrome", "brave"), start=False)
        w[main_dev].up()
        for fw in list(w[other].got):
            await w[other].finish(fw)
        await asyncio.sleep(7)
        rows = await live(c)
        stuck = [t for t in rows if t["worker"] == main_dev and not t["started_at"]]
        check("main got work back", len(stuck) > 0, True)
        await asyncio.sleep(12)
        rows = await live(c)
        check("never-started tasks moved off main",
              any(t["worker"] == main_dev and t["started_at"] is None
                  and time.time() - (t["sent_at"] or 0) > 9 for t in rows), False)

        # 8. persistence file has the live tasks
        with open(os.environ["GATE_TASKS_FILE"], "rb") as fh:
            raw = fh.read()
        check("tasks file encrypted (no email readable)", b"@x" in raw or b'"payload"' in raw, False)
        saved = json.loads(gate._FERNET.decrypt(raw))
        check("tasks persisted", len(saved), len(await live(c)))
        st = (await c.get(f"{BASE}/admin/status", headers=H)).json()
        print("load:", st["load"], "ready:", st["ready"])

        # ── batch_job ───────────────────────────────────────────────────────
        for t in await live(c):
            await c.post(f"{BASE}/admin/tasks/{t['task_id']}/cancel", headers=H)
        for fw in w.values():
            await fw.down()
        # the main reports no connected extension at all: still ready
        w = {main_dev: FakeWorker(main_dev, ext=()), other: FakeWorker(other)}
        for fw in w.values():
            fw.up()
        await asyncio.sleep(2)

        def row_of(rows, tid):
            return next((t for t in rows if t["task_id"] == tid), None)

        lid = await submit(c, "web_login", {"email": "noext@x"})
        check("login goes to a worker with no extension connected",
              row_of(await live(c), lid)["worker"], main_dev)
        await w[main_dev].finish(lid)
        await asyncio.sleep(0.5)

        # 9. every browser failed on main -> the other PC; failing there too -> error
        fid = await submit(c, "batch_job", {"action": "commit", "username": "neo", "job_id": 1})
        check("batch_job commit -> main", row_of(await live(c), fid)["worker"], main_dev)
        await w[main_dev].finish(fid, "retry", "lookup failed")
        await asyncio.sleep(0.5)
        check("batch_job retry -> other PC", row_of(await live(c), fid)["worker"], other)
        await w[other].finish(fid, "retry", "lookup failed")
        await asyncio.sleep(0.5)
        check("second batch_job retry -> failed", row_of(await live(c), fid), None)
        hist = (await c.get(f"{BASE}/admin/tasks", headers=H)).json()["history"]
        check("failed with the last error", (hist[0]["status"], hist[0]["error"]),
              ("error", "lookup failed"))

        # 10. a started commit whose device disconnects fails, never re-routed
        fid = await submit(c, "batch_job", {"action": "commit", "username": "neo", "job_id": 2})
        await asyncio.sleep(0.5)
        holder = row_of(await live(c), fid)["worker"]
        check("commit started", bool(row_of(await live(c), fid)["started_at"]), True)
        await w[holder].down()
        await asyncio.sleep(10)
        check("started commit not re-routed after disconnect", row_of(await live(c), fid), None)
        hist = (await c.get(f"{BASE}/admin/tasks", headers=H)).json()["history"]
        check("started commit failed as unknown payment",
              hist[0]["error"], "payment state unknown — check manually")

        # 11. payment_unconfirmed fails straight away, no retry
        w[holder] = FakeWorker(holder)
        w[holder].up()
        await asyncio.sleep(2)
        fid = await submit(c, "batch_job", {"action": "commit", "username": "neo", "job_id": 3})
        await asyncio.sleep(0.5)
        holder = row_of(await live(c), fid)["worker"]
        await w[holder].finish(fid, "error", "payment_unconfirmed: no verdict")
        await asyncio.sleep(0.5)
        check("payment_unconfirmed -> failed, no retry", row_of(await live(c), fid), None)
        other_got = [t for fw in w.values() for t in fw.got if t == fid]
        check("never sent to another device", other_got, [])

        # 12. worker restarts inside the grace with its saved queue: the gate
        # keeps what is still its own and drops the rest
        lid = await submit(c, "form_submit", {"email": "resume@x"})
        await asyncio.sleep(0.5)
        holder = row_of(await live(c), lid)["worker"]
        await w[holder].down()
        w[holder] = FakeWorker(holder, resume=[lid, "not-a-task"])
        w[holder].up()
        await asyncio.sleep(2.5)
        check("resume keeps only its own task", w[holder].kept, [lid])
        check("resumed task still on that device",
              (row_of(await live(c), lid) or {}).get("worker"), holder)
        await asyncio.sleep(4)
        check("not taken back after the grace",
              (row_of(await live(c), lid) or {}).get("worker"), holder)

        # 13. direct handoff: the other device claims a task that hasn't
        # started; a started one stays where it is
        def peer_of(d):
            return "2" if d == "1" else "1"
        await w[peer_of(holder)].claim(lid, holder)
        await asyncio.sleep(0.5)
        check("claim of a started task refused", w[peer_of(holder)].claims[-1:],
              [("claim_no", "already started")])
        for d in list(w):
            await w[d].down()
            w[d] = FakeWorker(d, start=False)
            w[d].up()
        await asyncio.sleep(2)
        nid = await submit(c, "form_submit", {"email": "handoff@x"})
        await asyncio.sleep(0.5)
        h = row_of(await live(c), nid)["worker"]
        o = peer_of(h)
        await w[o].claim(nid, h)
        await asyncio.sleep(0.5)
        check("claim accepted", w[o].claims[-1:], [("claim_ok", None)])
        check("task now on the claiming device", row_of(await live(c), nid)["worker"], o)
        check("claiming device got the task", nid in w[o].got, True)
        check("old device told to drop it", nid in w[h].got, False)
        await w[h].claim(nid, h)
        await asyncio.sleep(0.5)
        check("stale claim refused", (w[h].claims[-1:] or [("", "")])[0][0], "claim_no")
        check("still on the claimer", row_of(await live(c), nid)["worker"], o)

        # 14. daily stats counted every finished task (counts only)
        st = (await c.get(f"{BASE}/admin/stats?days=1", headers=H)).json()
        rows = next(iter(st["days"].values()))
        check("stats have rows", bool(rows), True)
        check("stats rows have no emails",
              any("@" in json.dumps(r) for r in rows), False)
        check("summary text", gate.stats.summary_text(next(iter(st["days"]))).startswith(
            "📈 Daily tasks"), True)


def main():
    for f in (os.environ["GATE_TASKS_FILE"],):
        if os.path.exists(f):
            os.remove(f)
    cfg = uvicorn.Config(gate.app, host="127.0.0.1", port=18102, log_level="warning")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    time.sleep(2)
    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        th.join(5)
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
