"""Offline checks for step frames, payment safety and cleanup alerts.
Run: python test_steps.py"""
import asyncio
import os
import tempfile

os.environ.update({
    "WORKER_TOKEN": "t", "ADMIN_KEY": "k", "CALLBACK_RETRIES": "1",
    "TASKS_KEY": "yZ0zX3TqE2C1bq3n0nY1bR1cC6o0m7H2d4u3pQ9aX1s=",
    "GATE_OUTBOX_FILE": os.path.join(tempfile.gettempdir(), "s_outbox.json"),
    "GATE_TASKS_FILE": os.path.join(tempfile.gettempdir(), "s_tasks.json"),
})

import main  # noqa: E402

fails = []
alerts = []


def check(name, got, want):
    if got != want:
        fails.append(name)
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {name}")


main.telegram_bot.notify_bg = lambda text, *_a, **_k: alerts.append(text)


async def no_telegram(*_a, **_k):
    return None
main.telegram_bot.send = no_telegram
main.telegram_bot.edit = no_telegram


async def delivered(task, status, result, error):
    task["delivered"] = (status, error)
main._deliver_callback = delivered


def new(service, action="", **extra):
    t = {"task_id": f"{service}-{action}-{len(main.tasks)}", "service": service,
         "payload": {"action": action, "job_id": 7, "email": "a@b.c"} if action
         else {"job_id": 7, "email": "a@b.c"},
         "state": main.SENT_HP, "worker": "1", "created": 1.0, "num": 5} | extra
    main.tasks[t["task_id"]] = t
    return t


async def run():
    s = new("web_payment")
    await main._handle_step({"task_id": s["task_id"], "name": "pay", "ok": True,
                             "paid": True, "note": "$1.99"}, "1")
    check("step saved", (s["last_step"], s["paid_step"]), ("pay", "pay"))
    check("step on the card", s["card_lines"][-1].endswith("step pay: ok — $1.99"), True)
    await main._handle_step({"task_id": s["task_id"], "name": "x"}, "2")
    check("step from another device ignored", s["last_step"], "pay")

    check("card head shows the type", main._card_text(s).splitlines()[0],
          "#5  web_payment | payment")
    s["ref"] = "29/12-5"
    check("card head uses the ref", main._card_text(s).splitlines()[0],
          "29/12-5  web_payment | payment")

    s["started_at"] = 10.0
    check("started web_payment = paying (never re-run)", main._paying(s), True)
    b = new("batch_job", "commit", started_at=10.0)
    check("started batch_job commit = paying", main._paying(b), True)
    c = new("batch_job", "check", started_at=10.0)
    check("started batch_job check may re-run", main._paying(c), False)
    login = new("web_login", started_at=10.0)
    check("started login may re-run", main._paying(login), False)

    result = {"outcome": "paid",
              "cleanup": {"undo_setup": {"ok": False}, "verify": {"ok": True},
                          "logout": {"ok": False}}}
    check("cleanup failures found", main._cleanup_failures(s, result),
          ["undo_setup failed", "logout failed"])
    check("clean result -> nothing", main._cleanup_failures(s, {"cleanup": {
        "undo_setup": {"ok": True}}}), [])

    alerts.clear()
    await main._handle_hp_result({"task_id": s["task_id"], "status": "done",
                                  "result": result}, "1")
    check("paid task still done", s["delivered"], ("done", None))
    check("one cleanup alert", len(alerts), 1)
    check("alert names the account and step",
          "account a@b.c" in alerts[0] and "undo_setup failed" in alerts[0], True)
    row = main.history[0]
    check("history: type, steps, cleanup",
          (row["type"], [x["name"] for x in row["steps"]], row["cleanup_needed"]),
          ("payment", ["pay"], ["undo_setup failed", "logout failed"]))
    res = await main.admin_cleanup_fixed(s["task_id"], x_admin_key="k")
    check("cleanup marked fixed", (res["ok"], bool(main.history[0].get("cleanup_fixed"))),
          (True, True))

    e = new("echo")
    await main._card_add(e, "received")
    check("test type posts no card", e.get("card_msg_id"), None)

    # priority at intake: a held commit (same Batch job cap) is ahead of a new check
    main.tasks.clear()
    commit = new("batch_job", "commit", state=main.HELD, worker=None, created=100.0)
    chk = new("batch_job", "check", state=main.HELD, worker=None, created=200.0)
    check("new check waits behind a held commit", main._waiting_ahead(chk), True)
    check("the commit has nobody ahead", main._waiting_ahead(commit), False)
    code = new("form_submit", state=main.HELD, worker=None, created=300.0)
    check("uncapped action never waits at intake", main._waiting_ahead(code), False)
    commit2 = new("batch_job", "commit", state=main.HELD, worker=None, created=50.0)
    check("an older commit is ahead of a newer commit", main._waiting_ahead(commit), True)
    routed = []

    async def fake_route(task):
        routed.append(task["task_id"])
    real_route = main._route
    main._route = fake_route
    await main._route_held("batch_job")
    main._route = real_route
    check("held batch_job offered slots in order", routed,
          [commit2["task_id"], commit["task_id"], chk["task_id"]])

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")


asyncio.run(run())
