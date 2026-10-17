"""Offline checks for the results outbox. Run: python test_outbox.py"""
import asyncio
import os
import tempfile

os.environ.update({
    "WORKER_TOKEN": "t", "ADMIN_KEY": "k", "CALLBACK_RETRIES": "1",
    "TASKS_KEY": "yZ0zX3TqE2C1bq3n0nY1bR1cC6o0m7H2d4u3pQ9aX1s=",
    "GATE_OUTBOX_FILE": os.path.join(tempfile.gettempdir(), "t_outbox.json"),
    "GATE_TASKS_FILE": os.path.join(tempfile.gettempdir(), "t_tasks.json"),
})

import main  # noqa: E402

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {name}")


class World:
    def __init__(self):
        self.t = 1000.0
        self.down = True
        self.posts = 0


w = World()
main.time.time = lambda: w.t
main.telegram_bot.notify_bg = lambda *_a, **_k: None


async def fake_post(url, body, tid):
    w.posts += 1
    return "connection refused" if w.down else ""
main._post_callback = fake_post


async def run():
    if os.path.exists(os.environ["GATE_OUTBOX_FILE"]):
        os.remove(os.environ["GATE_OUTBOX_FILE"])
    task = {"task_id": "t1", "service": "batch_job", "callback_url": "http://x/cb"}

    await main._deliver_callback(task, "done", {"gift": "ok"}, None)
    check("undelivered result kept", list(main._outbox), ["t1"])
    raw = open(os.environ["GATE_OUTBOX_FILE"], "rb").read()
    check("outbox file encrypted", b"gift" in raw, False)

    w.posts = 0
    await main._outbox_pass()
    check("not retried before its time", w.posts, 0)
    for _ in range(5):
        w.t += 400
        await main._outbox_pass()
    check("retried while down", main._outbox["t1"]["attempts"], 5)
    check("gap capped at 5 min", main._outbox_gap(20), 300.0)

    main._outbox.clear()
    check("restart reloads the outbox", main._outbox_load(), 1)

    w.down = False
    w.t += 1
    await main._outbox_pass()
    check("delivered once the caller is back", main._outbox, {})

    w.down = True
    await main._deliver_callback(task, "done", {"gift": "ok"}, None)
    w.t += main.OUTBOX_MAX_AGE_S + 1
    await main._outbox_pass()
    check("given up after 24 h", main._outbox, {})

    w.down = True
    await main._deliver_callback(task, "error", None, "x")
    w.down = False
    res = await main.admin_outbox_retry("t1", x_admin_key="k")
    check("manual retry delivers", res["delivered"], True)

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")


asyncio.run(run())
