"""task_types.py — every service belongs to a type; the rules live on the type.

router.py and main.py read rules_for(task) instead of checking service names.
A new service needs one line in SERVICES; an unknown one runs as "action".

Rule fields:
  cap                most tasks at once per device (None = no limit)
  cap_group          services sharing one cap (default: the service itself)
  place              fill_main | least_busy | online_only
  wake               may a task of this type wake a device
  retry              bounce (rejected login budget) | other_pc_once | none
  rerun_after_start  safe to run again once it started (False for money)
  run_limit          seconds a started task may run
  recheck            the worker re-checks the result before delivery
  card               post a Telegram task card
  browsers           browsers it may use (empty = the worker decides)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

LOGIN_CAP = int(os.getenv("LOGIN_CAP", "5"))
BATCH_CAP = int(os.getenv("BATCH_CAP", "3"))
PAYMENT_CAP = int(os.getenv("PAYMENT_CAP", "3"))

PLACES = ("fill_main", "least_busy", "online_only")
RETRIES = ("bounce", "other_pc_once", "none")


@dataclass(frozen=True)
class Rules:
    type: str
    cap: int | None
    place: str
    wake: bool
    retry: str
    rerun_after_start: bool
    run_limit: float
    recheck: bool = False
    card: bool = True
    cap_group: str | None = None
    browsers: tuple[str, ...] = field(default_factory=tuple)


TYPES: dict[str, Rules] = {
    "login": Rules("login", cap=LOGIN_CAP, place="fill_main", wake=True,
                   retry="bounce", rerun_after_start=True, run_limit=300.0,
                   browsers=("chrome", "vivaldi", "brave")),
    "payment": Rules("payment", cap=PAYMENT_CAP, place="fill_main", wake=True,
                     retry="other_pc_once", rerun_after_start=False,
                     run_limit=1800.0, recheck=True),
    "lookup": Rules("lookup", cap=None, place="online_only", wake=True,
                    retry="other_pc_once", rerun_after_start=True, run_limit=180.0),
    "action": Rules("action", cap=None, place="least_busy", wake=True,
                    retry="none", rerun_after_start=True, run_limit=180.0,
                    browsers=("brave",)),
    "test": Rules("test", cap=None, place="least_busy", wake=False,
                  retry="none", rerun_after_start=True, run_limit=300.0,
                  card=False),
}

# service -> type, or {payload action -> type} (first entry = the default action)
SERVICES: dict[str, str | dict[str, str]] = {
    "web_login": "login",
    "batch_job": {"check": "lookup", "commit": "payment"},
    "form_submit": "action",
    "profile_update": "action",
    "web_payment": "payment",
    "echo": "test",
    "ext_echo": "test",
}

# per-service changes to its type's rules
OVERRIDES: dict[str, dict] = {
    # commit and check share one cap; a check may take as long as a commit today
    # (three browsers plus the delivery re-checks)
    "batch_job": {"cap": BATCH_CAP, "cap_group": "batch_job", "run_limit": 1800.0},
    # one payment at a time: Brave only
    "web_payment": {"cap": 1, "browsers": ("brave",), "run_limit": 300.0},
}

DEFAULT_TYPE = "action"

# Held tasks at the gate and queued tasks on a worker are picked in this
# order; within one type, oldest first.
PRIORITY = ("login", "payment", "action", "lookup", "test")


def type_of(service: str, action: str = "") -> str:
    entry = SERVICES.get(service)
    if entry is None:
        return DEFAULT_TYPE
    if isinstance(entry, dict):
        return entry.get(action) or next(iter(entry.values()))
    return entry


def _action(task: dict) -> str:
    return str((task.get("payload") or {}).get("action") or "").lower()


def rules_for(task: dict | str) -> Rules:
    """The rules for one task (or a bare service name: its default action)."""
    if isinstance(task, str):
        service, action = task, ""
    else:
        service, action = task.get("service", ""), _action(task)
    rules = TYPES[type_of(service, action)]
    over = OVERRIDES.get(service) or {}
    if over:
        rules = replace(rules, **over)
    if rules.cap is not None and rules.cap_group is None:
        rules = replace(rules, cap_group=service)
    return rules


def priority(task: dict | str) -> int:
    """0 = picked first."""
    return PRIORITY.index(rules_for(task).type)


def pick_order(tasks: list[dict]) -> list[dict]:
    """Tasks in the order they should get a slot: type priority, then age."""
    return sorted(tasks, key=lambda t: (priority(t), float(t.get("created") or 0)))


def table() -> dict:
    """Types, their rules and the services in each (for /admin/status)."""
    out: dict = {}
    for name, r in TYPES.items():
        out[name] = {"rules": {k: v for k, v in r.__dict__.items() if k != "type"},
                     "services": []}
    for service, entry in SERVICES.items():
        actions = entry.items() if isinstance(entry, dict) else [("", entry)]
        for action, t in actions:
            out[t]["services"].append(f"{service}:{action}" if action else service)
    out["overrides"] = {s: dict(o) for s, o in OVERRIDES.items()}
    out["priority"] = list(PRIORITY)
    return out
