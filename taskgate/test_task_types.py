"""Table tests for task_types.py. Run: python test_task_types.py"""
import router
import task_types
from task_types import rules_for, type_of

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {name}")


def t(service, action=""):
    return {"service": service, "task_id": "x",
            "payload": {"action": action} if action else {}}


# service -> type
for (service, action), want in {
    ("web_login", ""): "login",
    ("batch_job", "commit"): "payment",
    ("batch_job", "check"): "lookup",
    ("batch_job", ""): "lookup",            # the worker's default action is check
    ("batch_job", "COMMIT"): "payment",
    ("form_submit", ""): "action",
    ("profile_update", ""): "action",
    ("web_payment", ""): "payment",
    ("echo", ""): "test",
    ("ext_echo", ""): "test",
    ("brand_new_service", ""): "action",
}.items():
    check(f"type {service}:{action}", rules_for(t(service, action)).type, want)
check("type_of unknown", type_of("nope"), "action")

# every rule field of every type (today's rules)
want_rules = {
    "login": (5, "fill_main", True, "bounce", True, 300.0, False, True),
    "payment": (3, "fill_main", True, "other_pc_once", False, 1800.0, True, True),
    "lookup": (None, "online_only", True, "other_pc_once", True, 180.0, False, True),
    "action": (None, "least_busy", True, "none", True, 180.0, False, True),
    "test": (None, "least_busy", False, "none", True, 300.0, False, False),
}
for name, want in want_rules.items():
    r = task_types.TYPES[name]
    check(f"rules {name}", (r.cap, r.place, r.wake, r.retry, r.rerun_after_start,
                            r.run_limit, r.recheck, r.card), want)
    check(f"{name}: valid place/retry",
          (r.place in task_types.PLACES, r.retry in task_types.RETRIES), (True, True))

# overrides and cap groups
commit, chk = rules_for(t("batch_job", "commit")), rules_for(t("batch_job", "check"))
check("batch_job commit + check share one cap group", (commit.cap_group, chk.cap_group), ("batch_job", "batch_job"))
check("batch_job cap 3 for both", (commit.cap, chk.cap), (3, 3))
check("batch_job run limit 30 min for both", (commit.run_limit, chk.run_limit), (1800.0, 1800.0))
check("batch_job commit never re-run after start", commit.rerun_after_start, False)
s = rules_for(t("web_payment"))
check("web_payment cap 1, Brave only, 5 min", (s.cap, s.browsers, s.run_limit), (1, ("brave",), 300.0))
check("web_payment own cap group", s.cap_group, "web_payment")
check("web_payment never re-run after start", s.rerun_after_start, False)
check("login cap group = the service", rules_for(t("web_login")).cap_group, "web_login")
check("uncapped: no cap group", rules_for(t("form_submit")).cap_group, None)
check("overrides don't leak into the type", task_types.TYPES["payment"].cap, 3)

# the router helpers take a task or a service name
check("cap_for task", router.cap_for(t("web_payment")), 1)
check("can_bounce batch_job check", router.can_bounce(t("batch_job", "check")), True)
check("can_bounce web_payment (other PC once)", router.can_bounce("web_payment"), True)
check("can_bounce echo", router.can_bounce("echo"), False)
check("snapshot cap groups", router.Router().snapshot()["cap_groups"],
      {"web_login": 5, "batch_job": 3, "web_payment": 1})

# a test task never wakes a device
r = router.Router()
d = r.choose(t("echo"), set(), set(), {})
check("echo, both off -> hold, no wake", (d["device"], d["wake"]), (None, None))
d = r.choose(t("form_submit") | {"task_id": "y"}, set(), set(), {})
check("action, both off -> wake", d["wake"] is not None, True)

# priority: login > payment > action > lookup > test, oldest first in a type
check("priority order", [task_types.priority(t(s, a)) for s, a in (
    ("web_login", ""), ("web_payment", ""), ("batch_job", "commit"), ("form_submit", ""),
    ("batch_job", "check"), ("echo", ""))], [0, 1, 1, 2, 3, 4])
mixed = [t("echo") | {"task_id": "e", "created": 1},
         t("batch_job", "check") | {"task_id": "c", "created": 2},
         t("form_submit") | {"task_id": "k", "created": 3},
         t("batch_job", "commit") | {"task_id": "b2", "created": 9},
         t("batch_job", "commit") | {"task_id": "b1", "created": 4},
         t("web_login") | {"task_id": "l", "created": 8}]
check("pick order", [x["task_id"] for x in task_types.pick_order(mixed)],
      ["l", "b1", "b2", "k", "c", "e"])

table = task_types.table()
check("table has the priority", table["priority"], ["login", "payment", "action", "lookup", "test"])
check("table lists batch_job:commit under payment", "batch_job:commit" in table["payment"]["services"], True)
check("table lists every service", sorted({s.split(":")[0] for k, v in table.items()
                                           if k not in ("overrides", "priority")
                                           for s in v["services"]}),
      sorted(task_types.SERVICES))

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")
for f in fails:
    print("FAIL", f)
raise SystemExit(1 if fails else 0)
