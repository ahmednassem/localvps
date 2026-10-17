"""A minimal worker for the task gate: it answers every task with its payload.

Real workers drive browsers; this one only shows the protocol, so you can run
the whole pipeline (backend -> task gate -> worker -> callback) on one
machine.

    pip install websockets
    TASKGATE_WS=ws://127.0.0.1:8080/ws/worker WORKER_TOKEN=change-me DEVICE_ID=1 \
        python echo_worker.py

Frames (JSON text over one WebSocket):

    gate -> worker   {"type": "task", "task_id", "service", "payload", "task_type", ...}
                     {"type": "cancel", "task_id"}
    worker -> gate   {"type": "accepted", "task_id"}      queued here
                     {"type": "started",  "task_id"}      running (never re-sent elsewhere)
                     {"type": "result",   "task_id", "status": "done"|"error", "result", "error"}
                     {"type": "status",   "loop_age", "task_ids", "browsers", "version"}
"""
import asyncio
import json
import os
import time

import websockets

URL = os.getenv("TASKGATE_WS", "ws://127.0.0.1:8080/ws/worker")
TOKEN = os.getenv("WORKER_TOKEN", "change-me")
DEVICE = os.getenv("DEVICE_ID", "1")
STATUS_EVERY_S = 10

held: set[str] = set()
loop_beat = time.time()


async def status(ws) -> None:
    await ws.send(json.dumps({
        "type": "status",
        "loop_age": round(time.time() - loop_beat, 1),
        "task_ids": sorted(held),
        "browsers": {},
        "version": "echo-1",
    }))


async def run_task(ws, msg: dict) -> None:
    tid = msg["task_id"]
    await ws.send(json.dumps({"type": "started", "task_id": tid}))
    await asyncio.sleep(1)
    held.discard(tid)
    await ws.send(json.dumps({"type": "result", "task_id": tid, "status": "done",
                              "result": {"echo": msg.get("payload")}, "error": None}))


async def session() -> None:
    global loop_beat
    async with websockets.connect(f"{URL}?token={TOKEN}&worker={DEVICE}") as ws:
        print("connected as device", DEVICE)

        async def beats() -> None:
            global loop_beat
            while True:
                loop_beat = time.time()
                await status(ws)
                await asyncio.sleep(STATUS_EVERY_S)

        beat = asyncio.create_task(beats())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "task":
                    held.add(msg["task_id"])
                    await ws.send(json.dumps({"type": "accepted", "task_id": msg["task_id"]}))
                    asyncio.create_task(run_task(ws, msg))
                elif msg.get("type") == "cancel":
                    held.discard(msg.get("task_id"))
                    await ws.send(json.dumps({"type": "cancelled", "task_id": msg.get("task_id")}))
        finally:
            beat.cancel()


async def main() -> None:
    delay = 2
    while True:
        started = time.time()
        try:
            await session()
        except (OSError, websockets.WebSocketException) as exc:
            print("link down:", exc)
        delay = 2 if time.time() - started > 30 else min(delay * 2, 60)
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
