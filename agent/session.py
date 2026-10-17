"""session.py — launch processes into the active interactive desktop session.

A LocalSystem service runs in session 0, which has no visible desktop. To make
AnyDesk (or any GUI) appear on the logged-in user's screen we grab the active
console/session user token and CreateProcessAsUser into it.

For apps that must run elevated (AnyDesk as Administrator), we prefer the
user's linked elevated token, then fall back to a one-shot Scheduled Task
with RunLevel=Highest (no UAC prompt when started by LocalSystem).
"""
from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("session")

try:
    import win32api          # type: ignore
    import win32con          # type: ignore
    import win32process      # type: ignore
    import win32profile      # type: ignore
    import win32security     # type: ignore
    import win32ts           # type: ignore
    _HAVE_WIN32 = True
except Exception:                                   # noqa: BLE001
    _HAVE_WIN32 = False

# TokenElevationType: Default=1, Full=2, Limited=3
_TokenElevationType = 18
_TokenLinkedToken = 19
_TokenElevationTypeLimited = 3


def _candidate_session_ids() -> list[int]:
    """Console first, then any active / connected user sessions (AnyDesk/RDP)."""
    ids: list[int] = []
    try:
        console = win32ts.WTSGetActiveConsoleSessionId()
        if console not in (0, 0xFFFFFFFF):
            ids.append(int(console))
    except Exception:                               # noqa: BLE001
        pass
    try:
        for s in win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE):
            sid = int(s["SessionId"])
            if sid == 0 or sid in ids:
                continue
            if s["State"] in (win32ts.WTSActive, win32ts.WTSConnected):
                ids.append(sid)
    except Exception:                               # noqa: BLE001
        pass
    return ids


def session_usable(session_id: int) -> bool:
    """True if WTS can hand us a user token (logged-in / unlocked desktop)."""
    if not _HAVE_WIN32 or session_id in (0, 0xFFFFFFFF):
        return False
    try:
        token = win32ts.WTSQueryUserToken(int(session_id))
        try:
            win32api.CloseHandle(token)
        except Exception:                           # noqa: BLE001
            pass
        return True
    except Exception:                               # noqa: BLE001
        return False


def usable_session_ids() -> list[int]:
    """Active sessions where GUI apps (display, AnyDesk) can launch."""
    return [sid for sid in _candidate_session_ids() if session_usable(sid)]


def has_usable_session() -> bool:
    return bool(usable_session_ids())


def _primary_from(user_token):
    return win32security.DuplicateTokenEx(
        user_token,
        win32security.SecurityImpersonation,
        win32con.MAXIMUM_ALLOWED,
        win32security.TokenPrimary,
        None,
    )


def _elevated_token(user_token):
    """Return an elevated primary token when the user is an admin (UAC split)."""
    try:
        elev_type = win32security.GetTokenInformation(user_token, _TokenElevationType)
        if elev_type == _TokenElevationTypeLimited:
            linked = win32security.GetTokenInformation(user_token, _TokenLinkedToken)
            # pywin32 may return the linked handle directly
            return _primary_from(linked)
    except Exception as exc:                        # noqa: BLE001
        log.debug("no linked elevated token: %s", exc)
    # Already full / default — still duplicate to a primary we can launch with
    return _primary_from(user_token)


def _create_as_user(token, cmdline: str, cwd: str | None, session_id: int,
                    mode: str) -> dict:
    env = win32profile.CreateEnvironmentBlock(token, False)
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = "winsta0\\default"
    flags = (win32con.CREATE_NEW_CONSOLE
             | win32con.CREATE_UNICODE_ENVIRONMENT)
    proc_handle, thread_handle, pid, tid = win32process.CreateProcessAsUser(
        token, None, cmdline, None, None, False, flags, env, cwd, startup)
    try:
        win32api.CloseHandle(proc_handle)
        win32api.CloseHandle(thread_handle)
    except Exception:                               # noqa: BLE001
        pass
    return {"ok": True, "pid": pid, "session": session_id, "mode": mode}


def _launch_as_user(cmdline: str, cwd: str | None, session_id: int) -> dict:
    user_token = win32ts.WTSQueryUserToken(session_id)
    primary = _primary_from(user_token)
    return _create_as_user(primary, cmdline, cwd, session_id, "as_user")


def _launch_elevated_token(cmdline: str, cwd: str | None, session_id: int) -> dict:
    user_token = win32ts.WTSQueryUserToken(session_id)
    primary = _elevated_token(user_token)
    return _create_as_user(primary, cmdline, cwd, session_id, "elevated_token")


def _session_user(session_id: int) -> tuple[str, str]:
    """Return (domain, username) for a session."""
    user = win32ts.WTSQuerySessionInformation(
        win32ts.WTS_CURRENT_SERVER_HANDLE, session_id, win32ts.WTSUserName)
    domain = win32ts.WTSQuerySessionInformation(
        win32ts.WTS_CURRENT_SERVER_HANDLE, session_id, win32ts.WTSDomainName)
    return (domain or "").strip(), (user or "").strip()


def _launch_elevated_task(cmdline: str, session_id: int) -> dict:
    """One-shot scheduled task at Highest run level in the interactive session."""
    domain, user = _session_user(session_id)
    if not user:
        raise RuntimeError(f"no username for session {session_id}")
    account = f"{domain}\\{user}" if domain else user
    task = "LocalServerAnyDeskAdmin"
    exe = cmdline.strip()
    if exe.startswith('"') and exe.endswith('"') and exe.count('"') == 2:
        exe = exe[1:-1]
    exe_ps = exe.replace("'", "''")
    account_ps = account.replace("'", "''")

    ps = f"""
$tn = '{task}'
$exe = '{exe_ps}'
if (-not (Test-Path -LiteralPath $exe)) {{ throw "exe not found: $exe" }}
$action = New-ScheduledTaskAction -Execute $exe
$principal = New-ScheduledTaskPrincipal -UserId '{account_ps}' `
  -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $tn -Action $action -Principal $principal `
  -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $tn
Start-Sleep -Seconds 2
Write-Output 'started'
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", ps],
        capture_output=True, text=True, timeout=60,
        creationflags=0x08000000,
    )
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0 or "started" not in out.lower():
        raise RuntimeError(f"elevated task failed: {out or r.returncode}")
    return {"ok": True, "session": session_id, "mode": "elevated_task", "output": out}


def launch_in_session(cmdline: str, cwd: str | None = None) -> dict:
    """Start cmdline in the active user session (normal integrity)."""
    if not _HAVE_WIN32:
        p = subprocess.Popen(cmdline, cwd=cwd, shell=True)
        return {"ok": True, "pid": p.pid, "mode": "subprocess"}

    errors: list[str] = []
    for session_id in _candidate_session_ids():
        try:
            return _launch_as_user(cmdline, cwd, session_id)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"session {session_id}: {exc}")

    detail = "; ".join(errors) if errors else "no candidate sessions"
    log.warning("launch_in_session failed (%s); trying plain spawn", detail)
    try:
        p = subprocess.Popen(cmdline, cwd=cwd, shell=True)
        return {"ok": True, "pid": p.pid, "mode": "fallback"}
    except Exception as exc2:                       # noqa: BLE001
        return {"ok": False, "error": str(exc2)}


def launch_elevated_in_session(cmdline: str, cwd: str | None = None) -> dict:
    """Start cmdline elevated (Administrator) on the interactive desktop.

    Order: linked elevated token → scheduled task Highest → normal session launch.
    """
    if not _HAVE_WIN32:
        p = subprocess.Popen(cmdline, cwd=cwd, shell=True)
        return {"ok": True, "pid": p.pid, "mode": "subprocess"}

    errors: list[str] = []
    sessions = _candidate_session_ids()
    for session_id in sessions:
        try:
            return _launch_elevated_token(cmdline, cwd, session_id)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"token session {session_id}: {exc}")

    for session_id in sessions:
        try:
            return _launch_elevated_task(cmdline, session_id)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"task session {session_id}: {exc}")

    detail = "; ".join(errors) if errors else "no candidate sessions"
    log.warning("elevated launch failed (%s); falling back to normal session", detail)
    return launch_in_session(cmdline, cwd)
