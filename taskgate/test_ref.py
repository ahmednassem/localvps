"""Offline checks for the task reference (local date + day number).
Run: python test_ref.py"""
import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

STATE = os.path.join(tempfile.gettempdir(), "r_state.json")
os.environ.update({
    "REF_TZ": "Europe/Athens", "WORKER_TOKEN": "t", "ADMIN_KEY": "k",
    "TASKS_KEY": "yZ0zX3TqE2C1bq3n0nY1bR1cC6o0m7H2d4u3pQ9aX1s=",
    "GATE_STATE_FILE": STATE,
    "GATE_OUTBOX_FILE": os.path.join(tempfile.gettempdir(), "r_outbox.json"),
    "GATE_TASKS_FILE": os.path.join(tempfile.gettempdir(), "r_tasks.json"),
})
if os.path.exists(STATE):
    os.remove(STATE)

import main  # noqa: E402

TZ_LOCAL = ZoneInfo("Europe/Athens")
fails = []


def check(name, got, want):
    if got != want:
        fails.append(name)
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {name}")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_LOCAL).timestamp()


main._ref_day, main._ref_seq = "", 0
check("first of the day", main._new_ref(at("2026-12-29T09:00")), "29/12-1")
check("second", main._new_ref(at("2026-12-29T12:00")), "29/12-2")
check("third, one second before midnight", main._new_ref(at("2026-12-29T23:59:59")), "29/12-3")
check("restarts after 00:00 local (winter)", main._new_ref(at("2026-12-30T00:00:01")), "30/12-1")
# summer time (UTC+3 in the test zone): 23:30 UTC is already the next day in local
main._ref_day, main._ref_seq = "", 0
summer = datetime.fromisoformat("2026-07-14T20:59:00+00:00").timestamp()   # 23:59 local
check("summer 23:59 local", main._new_ref(summer), "14/7-1")
check("summer 00:01 local = new day", main._new_ref(summer + 120), "15/7-1")

main._save_state()
main._ref_day, main._ref_seq = "", 0
main._load_state()
check("a restart continues the count", main._new_ref(summer + 600), "15/7-2")

refs = {main._new_ref(summer + 900) for _ in range(50)}
check("50 at once, all different", len(refs), 50)

check("normalise", [main._norm_ref(x) for x in ("29/12-5", " #29/12-05 ", "29-12-5")],
      ["29/12/5"] * 3)


async def run():
    main.tasks.clear()
    main.history.clear()
    main.tasks["a"] = {"task_id": "a", "service": "form_submit", "state": main.HELD,
                       "ref": "29/12-5", "payload": {}, "created": 1.0,
                       "callback_url": "http://x/cb"}
    main.history.appendleft({"task_id": "b", "ref": "29/12-4", "service": "batch_job",
                             "type": "payment", "status": "done", "worker": "1",
                             "created": 10.0, "finished_at": 75.0})
    r = await main.admin_tasks(ref="29/12-05", x_admin_key="k")
    check("lookup live by ref", [t["task_id"] for t in r["live"]], ["a"])
    r = await main.admin_tasks(ref="29/12-4", x_admin_key="k")
    check("lookup history by ref", [t["task_id"] for t in r["history"]], ["b"])
    check("telegram /task (history)", main._find_ref("29/12-4").splitlines()[:2],
          ["29/12-4  batch_job | payment", "done on device 1 after 1m5s"])
    check("telegram /task (live) card head", main._find_ref("29/12-5").splitlines()[0],
          "29/12-5  form_submit | action")
    check("unknown ref", main._find_ref("1/1-1").startswith("No task"), True)

    sent = []

    async def fake_post(url, body, tid):
        sent.append(body)
        return ""
    main._post_callback = fake_post
    await main._deliver_callback(main.tasks["a"], "done", {}, None)
    check("callback carries ref", sent[0].get("ref"), "29/12-5")

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")


asyncio.run(run())
