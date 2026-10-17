"""Daily task stats per type and device: count, success %, average time.

Only counts and durations are kept (no payloads), one entry per
local day in stats.json next to this file, for KEEP_DAYS days.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("stats")

PATH = os.getenv("GATE_STATS_FILE",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json"))
KEEP_DAYS = 60

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("UTC")
except Exception:                                    # noqa: BLE001
    TZ = timezone.utc

_days: dict[str, dict] = {}
_loaded = False


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def _load() -> None:
    global _days, _loaded
    _loaded = True
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _days = data
    except (OSError, ValueError):
        _days = {}


def _save() -> None:
    cutoff = day_of(time.time() - KEEP_DAYS * 86400)
    for d in [d for d in _days if d < cutoff]:
        _days.pop(d)
    tmp = PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_days, fh)
        os.replace(tmp, PATH)
    except OSError as exc:
        log.warning("saving stats failed: %s", exc)


def record(task_type: str, device, status: str, took_s: float | None,
           now: float | None = None) -> None:
    """One finished task. status: done | error | timeout | cancelled ..."""
    if not _loaded:
        _load()
    now = now or time.time()
    day = _days.setdefault(day_of(now), {})
    key = f"{task_type}|{device or '-'}"
    row = day.setdefault(key, {"n": 0, "ok": 0, "secs": 0.0, "timed": 0})
    row["n"] += 1
    if status == "done":
        row["ok"] += 1
    if took_s is not None and took_s >= 0:
        row["secs"] += float(took_s)
        row["timed"] += 1
    _save()


def day_rows(day: str) -> list[dict]:
    if not _loaded:
        _load()
    out = []
    for key, r in sorted((_days.get(day) or {}).items()):
        typ, dev = key.split("|", 1)
        out.append({"type": typ, "device": dev, "count": r["n"], "done": r["ok"],
                    "success_pct": round(100 * r["ok"] / r["n"]) if r["n"] else None,
                    "avg_s": round(r["secs"] / r["timed"]) if r["timed"] else None})
    return out


def days(n: int = 7, now: float | None = None) -> dict[str, list[dict]]:
    now = now or time.time()
    return {day_of(now - i * 86400): day_rows(day_of(now - i * 86400)) for i in range(n)}


def _fmt_s(s) -> str:
    if s is None:
        return "-"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def summary_text(day: str, rotation: dict | None = None) -> str:
    rows = day_rows(day)
    lines = [f"📈 Daily tasks {day}"]
    if not rows:
        lines.append("No tasks.")
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for typ, rs in by_type.items():
        n = sum(r["count"] for r in rs)
        ok = sum(r["done"] for r in rs)
        per_dev = ", ".join(
            f"device {r['device']} {r['count']} ({r['success_pct']}%, avg {_fmt_s(r['avg_s'])})"
            for r in rs)
        lines.append(f"• {typ}: {n}, {round(100 * ok / n) if n else 0}% ok — {per_dev}")
    if rotation:
        lines.append("Login rotation (since the gate started): " + ", ".join(
            f"device {d} {v.get('logins', 0)} logins, {v.get('switches', 0)} switches"
            + ("" if v.get("enabled") else " (off)")
            for d, v in sorted(rotation.items())))
    return "\n".join(lines)
