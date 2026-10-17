"""rustdesk_mgr.py — monitor and manage RustDesk on the laptop.

Mirror of anydesk_mgr.py for the self-hosted RustDesk client. Reads the ID via
`rustdesk.exe --get-id`, checks the process, and can kill / relaunch it
**as Administrator** into the user desktop session.
"""
from __future__ import annotations

import logging
import os

import psutil

import session
from anydesk_mgr import _pid_elevated, _pid_session_id, _user_session_ids
from config import Config
from winutil import run

log = logging.getLogger("rustdesk")

_PROC_NAMES = ("rustdesk.exe",)

_id_cache: str | None = None


def user_session_pids() -> list[int]:
    """rustdesk.exe in the logged-in desktop session (not session-0 service)."""
    want = _user_session_ids()
    out: list[int] = []
    for pid in pids():
        sid = _pid_session_id(pid)
        if sid is None:
            continue
        if want:
            if sid in want:
                out.append(pid)
        elif sid != 0:
            out.append(pid)
    return out


def is_running_in_user_session() -> bool:
    return bool(user_session_pids())


def is_running() -> bool:
    """True if RustDesk is running where it matters (user desktop if logged in)."""
    if _user_session_ids():
        return is_running_in_user_session()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in _PROC_NAMES:
                return True
        except Exception:                           # noqa: BLE001
            continue
    return False


def pids() -> list[int]:
    out = []
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if (p.info.get("name") or "").lower() in _PROC_NAMES:
                out.append(p.info["pid"])
        except Exception:                           # noqa: BLE001
            continue
    return out


async def read_id(cfg: Config | None = None) -> str | None:
    """RustDesk ID via `rustdesk.exe --get-id`, or RUSTDESK_ID in .env."""
    global _id_cache
    if cfg and cfg.rustdesk_id:
        return cfg.rustdesk_id.strip()
    if _id_cache:
        return _id_cache
    path = cfg.rustdesk_path if cfg else ""
    if not path or not os.path.isfile(path):
        return None
    code, out = await run(f'"{path}" --get-id', timeout=15)
    out = (out or "").strip().splitlines()
    got = out[-1].strip() if out else ""
    if code == 0 and got and got.replace(" ", "").isdigit():
        _id_cache = got.replace(" ", "")
        return _id_cache
    return None


def is_elevated() -> bool:
    """True if RustDesk in the user session is elevated (Administrator)."""
    check = user_session_pids() if _user_session_ids() else pids()
    if not check:
        return False
    for pid in check:
        elev = _pid_elevated(pid)
        if elev is True:
            return True
    return False


def needs_admin_restart() -> bool:
    """Running in user session but not elevated → restart as Administrator."""
    if not is_running():
        return False
    return not is_elevated()


def _kill_pids(pid_list: list[int]) -> None:
    for pid in pid_list:
        try:
            psutil.Process(pid).kill()
        except Exception:                           # noqa: BLE001
            pass


async def stop() -> dict:
    killed = pids()
    _kill_pids(killed)
    await run('taskkill /F /IM rustdesk.exe /T', timeout=15)
    return {"ok": True, "killed": killed}


async def stop_user_session() -> dict:
    """Kill only the desktop RustDesk — leave session-0 service alone."""
    killed = user_session_pids()
    _kill_pids(killed)
    if killed:
        await run(f'taskkill /F /PID {" /PID ".join(str(p) for p in killed)} /T',
                  timeout=15)
    return {"ok": True, "killed": killed}


async def start(cfg: Config) -> dict:
    import asyncio
    if is_running() and is_elevated():
        return {"ok": True, "output": "already running (admin)",
                "id": await read_id(cfg), "elevated": True}
    if is_running() and not is_elevated():
        log.warning("RustDesk running without admin — restarting elevated")
        if _user_session_ids():
            await stop_user_session()
        else:
            await stop()
        await asyncio.sleep(2)

    path = cfg.rustdesk_path
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"RustDesk not found: {path}"}

    res = session.launch_elevated_in_session(f'"{path}"')
    await asyncio.sleep(2)
    res["id"] = await read_id(cfg)
    res["running"] = is_running()
    res["elevated"] = is_elevated()
    res["user_pids"] = user_session_pids()
    if res.get("ok") and res["running"]:
        admin = "admin" if res["elevated"] else "NOT elevated"
        res["output"] = (
            f"RustDesk started ({admin}, id={res.get('id')}, "
            f"pid={res.get('pid')}, mode={res.get('mode')})"
        )
        if not res["elevated"]:
            res["ok"] = False
            res["error"] = "started but process is not elevated"
    return res


async def restart(cfg: Config) -> dict:
    import asyncio
    await stop()
    await asyncio.sleep(2)
    return await start(cfg)


async def status(cfg: Config) -> dict:
    return {
        "ok": True,
        "running": is_running(),
        "elevated": is_elevated() if is_running() else False,
        "id": await read_id(cfg),
        "pids": user_session_pids() if _user_session_ids() else pids(),
        "path": cfg.rustdesk_path,
    }
