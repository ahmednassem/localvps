"""anydesk_mgr.py — monitor and manage AnyDesk on the laptop.

Reads the AnyDesk ID from ProgramData config, checks the process, and can kill /
relaunch it **as Administrator** into the user desktop session.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
from ctypes import wintypes

import psutil

import session
from config import Config
from winutil import run

log = logging.getLogger("anydesk")

_PROC_NAMES = ("anydesk.exe",)

TOKEN_QUERY = 0x0008
TokenElevation = 20


def _user_session_ids() -> set[int]:
    try:
        return set(session._candidate_session_ids())  # noqa: SLF001
    except Exception:                               # noqa: BLE001
        return set()


def _pid_session_id(pid: int) -> int | None:
    try:
        return int(psutil.Process(pid).session_id())
    except Exception:                               # noqa: BLE001
        pass
    try:
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
            return int(sid.value)
    except Exception:                               # noqa: BLE001
        pass
    return None


def user_session_pids() -> list[int]:
    """AnyDesk.exe in the logged-in desktop session (not session-0 service)."""
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
    """True if AnyDesk is running where it matters (user desktop if logged in)."""
    if _user_session_ids():
        return is_running_in_user_session()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in _PROC_NAMES:
                return True
        except Exception:                           # noqa: BLE001
            continue
    return False


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def _conf_paths() -> list[str]:
    pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    appd = os.environ.get("APPDATA", "")
    paths = [
        os.path.join(pd, "AnyDesk", "system.conf"),
        os.path.join(pd, "AnyDesk", "user.conf"),
    ]
    if appd:
        paths.append(os.path.join(appd, "AnyDesk", "user.conf"))
        paths.append(os.path.join(appd, "AnyDesk", "system.conf"))
    return paths


def read_id(cfg: Config | None = None) -> str | None:
    """AnyDesk address / ID from config files or ANYDESK_ID in .env."""
    if cfg and cfg.anydesk_id:
        return cfg.anydesk_id.strip()
    for path in _conf_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("ad.anynet.id="):
                        return line.split("=", 1)[1].strip()
                    m = re.search(r"ad\.anynet\.id\s*=\s*(\d+)", line)
                    if m:
                        return m.group(1)
        except Exception as exc:                    # noqa: BLE001
            log.debug("read %s: %s", path, exc)
    return None


def pids() -> list[int]:
    out = []
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if (p.info.get("name") or "").lower() in _PROC_NAMES:
                out.append(p.info["pid"])
        except Exception:                           # noqa: BLE001
            continue
    return out


def _pid_elevated(pid: int) -> bool | None:
    """True if process token is elevated; None if unknown."""
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        th = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(th)):
            return None
        try:
            te = _TOKEN_ELEVATION()
            needed = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                th, TokenElevation, ctypes.byref(te),
                ctypes.sizeof(te), ctypes.byref(needed),
            )
            if not ok:
                return None
            return bool(te.TokenIsElevated)
        finally:
            kernel32.CloseHandle(th)
    finally:
        kernel32.CloseHandle(h)


def is_elevated() -> bool:
    """True if AnyDesk in the user session is elevated (Administrator)."""
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
    await run('taskkill /F /IM AnyDesk.exe /T', timeout=15)
    return {"ok": True, "killed": killed}


async def stop_user_session() -> dict:
    """Kill only the desktop AnyDesk — leave session-0 service alone."""
    killed = user_session_pids()
    _kill_pids(killed)
    if killed:
        await run(f'taskkill /F /PID {" /PID ".join(str(p) for p in killed)} /T',
                  timeout=15)
    return {"ok": True, "killed": killed}


async def start(cfg: Config) -> dict:
    if is_running() and is_elevated():
        return {"ok": True, "output": "already running (admin)", "id": read_id(cfg),
                "elevated": True}
    if is_running() and not is_elevated():
        log.warning("AnyDesk running without admin — restarting elevated")
        if _user_session_ids():
            await stop_user_session()
        else:
            await stop()
        import asyncio
        await asyncio.sleep(2)

    path = cfg.anydesk_path
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"AnyDesk not found: {path}"}

    res = session.launch_elevated_in_session(f'"{path}"')
    import asyncio
    await asyncio.sleep(2)
    res["id"] = read_id(cfg)
    res["running"] = is_running()
    res["elevated"] = is_elevated()
    res["user_pids"] = user_session_pids()
    if res.get("ok") and res["running"]:
        admin = "admin" if res["elevated"] else "NOT elevated"
        res["output"] = (
            f"AnyDesk started ({admin}, id={res.get('id')}, "
            f"pid={res.get('pid')}, mode={res.get('mode')})"
        )
        if not res["elevated"]:
            res["ok"] = False
            res["error"] = "started but process is not elevated"
    return res


async def restart(cfg: Config) -> dict:
    await stop()
    import asyncio
    await asyncio.sleep(2)
    return await start(cfg)


async def status(cfg: Config) -> dict:
    return {
        "ok": True,
        "running": is_running(),
        "elevated": is_elevated() if is_running() else False,
        "id": read_id(cfg),
        "pids": user_session_pids() if _user_session_ids() else pids(),
        "path": cfg.anydesk_path,
    }
