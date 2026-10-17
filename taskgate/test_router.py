"""Offline tests for the task router. Run: python test_router.py"""
import router

router.LOGIN_CAP = 5
router.BATCH_CAP = 3
router.WAKE_TIMEOUT_S = 240
router.LOGIN_MAX_ATTEMPTS = 10
router.LOGIN_MAX_TOTAL_S = 1800
router.MAIN_SWAP_S = 6 * 3600

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got}, want {want}")
    else:
        print(f"ok  {name}")


def task(service, action="", tid="t"):
    return {"task_id": tid, "service": service,
            "payload": {"action": action} if action else {}, "created": 1000.0}


def pick(d):
    return (d["device"], d["wake"])


def main():
    now = [1000.0]                      # slot 0 -> main is device 1
    real_time = router.time.time
    router.time.time = lambda: now[0]
    both = {"1", "2"}

    # ── logins: fill the main to 5, then the other to 5, then hold ────────
    r = router.Router()
    load = {"1": 0, "2": 0}
    got = []
    for i in range(11):
        d = r.choose(task("web_login", tid=f"l{i}"), both, both, load)
        got.append(d["device"])
        if d["device"]:
            load[d["device"]] += 1
    check("login fill 5 main then 5 other then hold",
          got, ["1"] * 5 + ["2"] * 5 + [None])
    # a slot frees on the main: the held one goes there
    load["1"] -= 1
    d = r.choose(task("web_login", tid="l10"), both, both, load)
    check("held login takes the freed slot", d["device"], "1")

    # main full, other off: wake the other and hold
    r = router.Router()
    d = r.choose(task("web_login", tid="w1"), {"1"}, {"1"}, {"1": 5})
    check("main full, other off -> wake other", pick(d), (None, "2"))
    # the woken device comes up: the task goes to it
    d = r.choose(task("web_login", tid="w1"), both, both, {"1": 5, "2": 0})
    check("woken device takes it", d["device"], "2")

    # main off, other up: the other works and the main is woken
    r = router.Router()
    d = r.choose(task("web_login", tid="m1"), {"2"}, {"2"}, {"2": 0})
    check("main off -> other works, main woken", pick(d), ("2", "1"))

    # main online but its extension isn't up yet: wait for it, no wake
    r = router.Router()
    t = task("web_login", tid="b1")
    d = r.choose(t, set(), {"1"}, {})
    check("main booting, other off -> wait for main", pick(d), (None, None))
    now[0] += router.WAKE_TIMEOUT_S + 1
    d = r.choose(t, set(), {"1"}, {})
    check("main never ready -> wake other", pick(d), (None, "2"))
    now[0] = 1000.0

    # ── both offline: wake the shift (main) device, even if the other has
    #    rested longer ──────────────────────────────────────────────────
    r = router.Router()
    r.note_offline("2"); now[0] += 10
    r.note_offline("1"); now[0] += 5000
    d = r.choose(task("web_login", tid="zl"), set(), set(), {})
    check("both down, login -> wake the shift device 1", pick(d), (None, "1"))
    six_h_ = router.MAIN_SWAP_S
    now[0] = 1000.0 + six_h_
    r = router.Router()
    r.note_offline("1"); now[0] += 10
    r.note_offline("2"); now[0] += 60
    d = r.choose(task("form_submit", tid="zc"), set(), set(), {})
    check("both down, next shift -> wake device 2", pick(d), (None, "2"))
    now[0] = 1000.0
    r = router.Router()
    r.note_offline("2"); now[0] += 10
    r.note_offline("1"); now[0] += 5000
    t = task("form_submit", tid="z")
    d = r.choose(t, set(), set(), {})
    check("both down, code -> wake the shift device 1", pick(d), (None, "1"))
    d = r.choose(t, set(), set(), {})
    check("still waiting, no second wake", pick(d), (None, None))
    now[0] += router.WAKE_TIMEOUT_S + 1
    d = r.choose(t, set(), set(), {})
    check("wake timeout -> wake the other", pick(d), (None, "2"))
    now[0] = 1000.0

    # ── batch_job ─────────────────────────────────────────────────────────────
    r = router.Router()
    load = {"1": 0, "2": 0}
    got = []
    for i in range(7):
        d = r.choose(task("batch_job", "commit", f"f{i}"), both, both, load)
        got.append(d["device"])
        if d["device"]:
            load[d["device"]] += 1
    check("batch_job commit fill 3 then 3 then hold", got, ["1"] * 3 + ["2"] * 3 + [None])
    d = r.choose(task("batch_job", "check", "c1"), {"2"}, {"2"}, {"2": 0})
    check("batch_job check -> online device, no wake", pick(d), ("2", None))
    d = r.choose(task("batch_job", "check", "c2"), {"2"}, {"2"}, {"2": 3})
    check("batch_job check, online device full -> hold, no wake", pick(d), (None, None))
    d = r.choose(task("batch_job", "check", "c3"), set(), set(), {})
    check("batch_job check, both down -> wake one", d["device"] is None and d["wake"] is not None, True)
    d = r.choose(task("batch_job", "commit", "f9"), {"2"}, {"2"}, {"2": 3})
    check("batch_job commit, online full -> wake the other", pick(d), (None, "1"))

    # ── uncapped: less busy, ties to the main ─────────────────────────────
    r = router.Router()
    d = r.choose(task("form_submit", tid="k1"), both, both, {"1": 0, "2": 0})
    check("code tie -> main", d["device"], "1")
    d = r.choose(task("form_submit", tid="k2"), both, both, {"1": 2, "2": 0})
    check("code -> less busy", d["device"], "2")
    # web_payment is a payment with cap 1 (one payment per device)
    d = r.choose(task("web_payment", tid="k3"), both, both, {"1": 1, "2": 0})
    check("web_payment cap 1: main busy -> other", d["device"], "2")
    d = r.choose(task("web_payment", tid="k3b"), both, both, {"1": 1, "2": 1})
    check("web_payment cap 1: both busy -> hold", d["device"], None)
    d = r.choose(task("profile_update", tid="k4"), {"2"}, both, {})
    check("profile_update -> the ready one, no wake", pick(d), ("2", None))

    # ── bounce ─────────────────────────────────────────────────────────────
    r = router.Router()
    bt = task("web_login", tid="bn")
    bt["created"] = now[0]
    bt["worker"] = "1"
    check("bounce allowed", r.bounce(bt), True)
    d = r.choose(bt, both, both, {"1": 0, "2": 0})
    check("bounce -> other device", d["device"], "2")
    bt["worker"] = "1"
    r.bounce(bt)
    d = r.choose(bt, {"1"}, {"1"}, {"1": 0})
    check("bounce, other off -> wake it, not the one that rejected it", pick(d), (None, "2"))
    now[0] += router.WAKE_TIMEOUT_S + 1
    d = r.choose(bt, {"1"}, {"1"}, {"1": 0})
    check("other never came -> same device retries", d["device"], "1")
    now[0] = 1000.0
    bt["attempts"] = 9
    check("bounce attempt budget spent", r.bounce(bt), False)
    bt["attempts"] = 2
    bt["created"] = now[0] - 99999
    check("bounce time budget spent", r.bounce(bt), False)

    # ── batch_job: one other-PC try, then stop ───────────────────────────────
    r = router.Router()
    ft = task("batch_job", "commit", "fb")
    ft["created"] = now[0]
    ft["worker"] = "1"
    check("batch_job may bounce once", r.bounce(ft), True)
    d = r.choose(ft, both, both, {"1": 0, "2": 0})
    check("batch_job bounce -> other PC", d["device"], "2")
    ft["worker"] = "2"
    check("batch_job second failure stops", r.bounce(ft), False)
    check("batch_job run limit 30 min", router.run_limit("batch_job"), 1800.0)
    check("batch_job commit detected", router.is_batch_commit(ft), True)
    check("code can't bounce", router.can_bounce("form_submit"), False)

    # ── main swap ──────────────────────────────────────────────────────────
    six_h = router.MAIN_SWAP_S
    check("main in slot 0", router.main_device(1000.0), "1")
    check("main swaps after 6 h", router.main_device(1000.0 + six_h), "2")
    check("main swaps back after 12 h", router.main_device(1000.0 + 2 * six_h), "1")

    # swaps land on 00/06/12/18 local time (summer and winter time)
    from datetime import datetime
    tz_local = router.SWAP_TZ
    for day in ("2026-09-27", "2026-12-15"):
        at = {h: datetime.fromisoformat(f"{day}T{h:02d}:00").replace(tzinfo=tz_local).timestamp()
              for h in (0, 6, 12, 18)}
        mains = [router.main_device(at[h] + 60) for h in (0, 6, 12, 18)]
        check(f"{day}: main alternates at 00/06/12/18 local",
              [mains[0] != mains[1], mains[1] != mains[2], mains[2] != mains[3]],
              [True, True, True])
        check(f"{day}: no swap mid-slot", router.main_device(at[6] - 60), mains[0])
        check(f"{day}: 03:11 -> until 06:00 local",
              router.main_until(at[0] + 3 * 3600 + 660), at[6])
    now[0] = 1000.0 + six_h
    r = router.Router()
    d = r.choose(task("web_login", tid="s1"), both, both, {"1": 0, "2": 0})
    check("login -> main 2 after the swap", d["device"], "2")
    d = r.choose(task("web_login", tid="s2"), {"1"}, {"1"}, {"1": 0})
    check("new main off -> old main works, new main woken", pick(d), ("1", "2"))
    check("snapshot shows main", r.snapshot()["main"], "2")

    # ── helpers ────────────────────────────────────────────────────────────
    check("cap group login", router.cap_group("web_login"), "web_login")
    check("no cap for code", router.cap_for("form_submit"), None)
    router.time.time = real_time
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED")
    for f in fails:
        print("FAIL", f)
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(main())
