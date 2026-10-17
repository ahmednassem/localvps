"""router_mgr.py — reboot the network boxes and report the public IP.

Three boxes provide internet in the new topology, each with its OWN WAN:

  dsl  the main DSL router (ROUTER_DSL_HOST). Serves the `wifi` path only.
  eth  the NEW router on the Ethernet cable. Reboot support is a placeholder
       until its admin UI is discovered at deploy (ROUTER_ETH_HOST/USER/PASS).
  sms  the 4G box (ROUTER_SMS_HOST). Serves the `sms` path (its
       Wi-Fi hotspot; USB tether as fallback).

A reboot is only ever safe while the laptop sits on a *different* box.
`reboot()` enforces that interlock itself: it moves the route to a surviving
path first, refuses if the survivor has no internet, and never lets two
reboots overlap.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

import httpx

import netctl
from config import Config, HERE

log = logging.getLogger("router")

# Which network paths die when a given box goes down (one WAN each now).
BOX_PATHS = {"dsl": ("wifi",), "eth": ("eth",), "sms": ("sms",)}
# Preferred parking order while a box reboots (first candidate that is
# actually up wins — see _pick_survivor). eth first for a DSL reboot because
# the cable does not involve the shared Wi-Fi radio.
BOX_SURVIVORS = {
    "dsl": ("eth", "sms"),
    "eth": ("wifi", "sms"),
    "sms": ("wifi", "eth"),
}

_IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com")

_SMS_FIELDS_CACHE = os.path.join(HERE, "logs", "sms_login_fields.json")

# One reboot at a time, machine-wide. The user's hard rule: never restart both
# boxes together — if both are down the laptop has no way back online.
_reboot_lock = asyncio.Lock()
_last_reboot: dict[str, float] = {}
# While a reboot runs the path is deliberately "wrong". net_watchdog and the
# Wi‑Fi warm check read this so they don't fight the interlock or fire alerts.
_suppress_until = 0.0
_current_target = ""


def reboot_in_progress() -> str:
    """Name of the box being rebooted right now, or "" if none."""
    if _suppress_until and time.monotonic() < _suppress_until:
        return _current_target or "?"
    return ""


def _suppress(seconds: float, target: str) -> None:
    global _suppress_until, _current_target
    _suppress_until = time.monotonic() + seconds
    _current_target = target


def _release() -> None:
    global _suppress_until, _current_target
    _suppress_until = 0.0
    _current_target = ""


async def public_ip(timeout: float = 8.0) -> str | None:
    """Current public IP, or None if every service failed."""
    async with httpx.AsyncClient(timeout=timeout,
                                 follow_redirects=True) as client:
        for url in _IP_SERVICES:
            try:
                r = await client.get(url)
                ip = (r.text or "").strip()
                if r.status_code == 200 and ip.count(".") == 3 and len(ip) <= 15:
                    return ip
            except Exception:                       # noqa: BLE001
                continue
    return None


async def _lan_alive(host: str, timeout: float = 4.0) -> bool:
    """TCP-connect to the box's web UI — is it back on the wire?"""
    for port in (443, 80):
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:                       # noqa: BLE001
                pass
            del reader
            return True
        except Exception:                           # noqa: BLE001
            continue
    return False


# --------------------------------------------------------------------------- #
# dsl — the DSL router's web admin
# --------------------------------------------------------------------------- #
# The UI's reboot button is not a form post. mngt_reboot.shtml does:
#   uiReboot() -> top.ConfirmMsg(..., "reboot")
#     -> ConfirmMsgResult("yes", "reboot")
#       -> document.location.href = "/page/management/mngt_restart.shtml"
# and mngt_restart.shtml is only a progress bar whose JS, two seconds in,
# fires   Ajax GET /cgi-bin/main.cgi?ops=reboot   — THAT is the reboot.
# (Verified: fetching mngt_restart.shtml alone leaves DeviceRun climbing.)
# The <form action="/cgi-bin/setup.cgi?..."> on mngt_reboot.shtml is a decoy.
DSL_RESTART_PATH = "/cgi-bin/main.cgi?ops=reboot"
DSL_LOGOUT_PATH = "/cgi-bin/logout.cgi"


async def _dsl_client(cfg: Config) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=f"https://{cfg.router_dsl_host}",
                             verify=False, timeout=20.0, follow_redirects=True)


async def _dsl_login(cfg: Config, client: httpx.AsyncClient) -> dict:
    """Log in. Sessions are keyed by source IP, not cookies, and the box allows
    exactly one admin at a time — so a human logged into the UI blocks us."""
    pw = hashlib.sha256(cfg.router_dsl_pass.encode()).hexdigest()
    r = await client.post("/", data={"username": cfg.router_dsl_user,
                                     "password": pw})
    url = str(r.url)
    if "login_succ" in url:
        return {"ok": True}
    if "login_fail_admin" in url or "logined" in url:
        return {"ok": False, "error": "router UI already has an admin session "
                                      "(log out of the web UI and retry)"}
    if "outtimes" in url:
        return {"ok": False, "error": "router locked us out (too many logins, "
                                      "wait 1 minute)"}
    return {"ok": False, "error": f"login failed ({r.status_code}, {url})"}


async def _dsl_trigger_reboot(cfg: Config) -> dict:
    client = await _dsl_client(cfg)
    fired = False
    try:
        got = await _dsl_login(cfg, client)
        if not got.get("ok"):
            return got
        try:
            r = await client.get(DSL_RESTART_PATH)
            log.info("dsl reboot cgi -> %s %r", r.status_code, r.text[:80])
        except Exception as exc:                    # noqa: BLE001
            # The box often drops the socket mid-response as it goes down;
            # that is a successful reboot, not a failure.
            log.info("dsl reboot request ended with %s (expected)",
                     type(exc).__name__)
        fired = True
        return {"ok": True}
    finally:
        # Release the single admin session only if we did NOT reboot — after
        # a reboot the session is gone anyway, and we must not race the cgi.
        if not fired:
            try:
                await client.get(DSL_LOGOUT_PATH)
            except Exception:                       # noqa: BLE001
                pass
        await client.aclose()


# --------------------------------------------------------------------------- #
# sms — the 4G box, single JSON endpoint /cgi-bin/http.cgi
# --------------------------------------------------------------------------- #
SMS_CGI = "/cgi-bin/http.cgi"
SMS_CMD_LOGIN = 100
SMS_CMD_LOGOUT = 101
SMS_CMD_REBOOT = 99
# Field naming differs between OEM builds of this firmware; the first variant
# that authenticates is cached so we only pay the discovery cost once.
SMS_LOGIN_VARIANTS = (
    ("user", "passwd"),
    ("username", "password"),
    ("user_name", "user_pwd"),
    ("Username", "Password"),
)


def _sms_cached_fields() -> tuple[str, str] | None:
    try:
        with open(_SMS_FIELDS_CACHE, encoding="utf-8") as fh:
            got = json.load(fh)
        return got["user_field"], got["pass_field"]
    except Exception:                               # noqa: BLE001
        return None


def _sms_cache_fields(user_field: str, pass_field: str) -> None:
    try:
        os.makedirs(os.path.dirname(_SMS_FIELDS_CACHE), exist_ok=True)
        with open(_SMS_FIELDS_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"user_field": user_field, "pass_field": pass_field}, fh)
    except Exception:                               # noqa: BLE001
        pass


async def _sms_call(cfg: Config, client: httpx.AsyncClient, cmd: int,
                    extra: dict | None = None, session_id: str = "",
                    token: str = "") -> dict:
    body = {"cmd": cmd, "method": "POST", "sessionId": session_id,
            "token": token}
    body.update(extra or {})
    url = f"{SMS_CGI}?cmd={cmd}&method=POST&sessionId={session_id}"
    r = await client.post(url, json=body)
    try:
        return r.json()
    except Exception:                               # noqa: BLE001
        return {"success": False, "message": f"non-json ({r.status_code})"}


async def _sms_login(cfg: Config, client: httpx.AsyncClient) -> dict:
    order = list(SMS_LOGIN_VARIANTS)
    cached = _sms_cached_fields()
    if cached:
        order.insert(0, cached)
    last = ""
    for user_field, pass_field in order:
        try:
            got = await _sms_call(cfg, client, SMS_CMD_LOGIN,
                                  {user_field: cfg.router_sms_user,
                                   pass_field: cfg.router_sms_pass})
        except Exception as exc:                    # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            continue
        if got.get("success"):
            sid = str(got.get("sessionId") or got.get("session_id") or "")
            tok = str(got.get("token") or "")
            _sms_cache_fields(user_field, pass_field)
            log.info("sms login ok via %s/%s", user_field, pass_field)
            return {"ok": True, "session_id": sid, "token": tok}
        last = str(got.get("message") or got)
    return {"ok": False, "error": f"sms login failed ({last})"}


async def _sms_trigger_reboot(cfg: Config) -> dict:
    client = httpx.AsyncClient(base_url=f"https://{cfg.router_sms_host}",
                               verify=False, timeout=20.0,
                               follow_redirects=True)
    try:
        got = await _sms_login(cfg, client)
        if not got.get("ok"):
            return got
        sid, tok = got["session_id"], got["token"]
        try:
            res = await _sms_call(cfg, client, SMS_CMD_REBOOT,
                                  session_id=sid, token=tok)
            if not res.get("success") and res.get("message"):
                return {"ok": False, "error": f"sms reboot refused "
                                              f"({res['message']})"}
        except Exception as exc:                    # noqa: BLE001
            log.info("sms reboot request ended with %s (expected)",
                     type(exc).__name__)
        return {"ok": True}
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# eth — the NEW router (admin UI not discovered yet)
# --------------------------------------------------------------------------- #
async def _eth_trigger_reboot(cfg: Config) -> dict:
    if not cfg.router_eth_host:
        return {"ok": False, "error": "ROUTER_ETH_HOST not configured"}
    return {"ok": False,
            "error": "new-router reboot not implemented yet — its admin UI "
                     "still has to be discovered (login by hand once, then "
                     "add the trigger here like _dsl_trigger_reboot)"}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
_TRIGGERS = {"dsl": _dsl_trigger_reboot, "eth": _eth_trigger_reboot,
             "sms": _sms_trigger_reboot}


def _host_for(cfg: Config, target: str) -> str:
    return {"dsl": cfg.router_dsl_host,
            "eth": cfg.router_eth_host,
            "sms": cfg.router_sms_host}.get(target, "")


async def _pick_survivor(cfg: Config, target: str) -> tuple[str | None, str]:
    """First surviving path (not served by `target`) that is actually up."""
    reasons = []
    for cand in BOX_SURVIVORS[target]:
        up = await netctl.ensure_path_up(cfg, cand)
        if up is None:
            return cand, ""
        reasons.append(f"{cand}: {up.get('error')}")
    return None, "; ".join(reasons)


async def _wait_back(cfg: Config, target: str, wait_s: float) -> bool:
    """Poll the box's LAN address until its web UI answers again."""
    deadline = time.monotonic() + wait_s
    host = _host_for(cfg, target)
    while time.monotonic() < deadline:
        if await _lan_alive(host):
            return True
        await asyncio.sleep(5.0)
    return False


async def reboot(cfg: Config, target: str = "dsl", *,
                 wait_s: float | None = None, force: bool = False,
                 restore: bool = True) -> dict:
    """Reboot a network box, keeping the laptop online throughout.

    Moves the route onto the surviving box first, aborts if that box has no
    internet, and refuses to run while another reboot is in flight.
    """
    target = (target or "dsl").lower().strip()
    if target in ("wifi", "router", "main"):
        target = "dsl"
    if target not in _TRIGGERS:
        return {"ok": False, "error": "target must be dsl|eth|sms"}
    if wait_s is None:
        wait_s = cfg.router_reboot_wait

    busy = reboot_in_progress()
    if busy or _reboot_lock.locked():
        return {"ok": False, "error": f"a {busy or 'router'} reboot is already "
                                      f"running — never reboot both boxes at once"}

    since = time.monotonic() - _last_reboot.get(target, -1e9)
    if not force and since < cfg.router_reboot_min_interval:
        left = int(cfg.router_reboot_min_interval - since)
        return {"ok": False, "error": f"{target} rebooted {int(since)}s ago; "
                                      f"{left}s of cooldown left (use force)"}

    async with _reboot_lock:
        doomed = BOX_PATHS[target]
        started_on, _ = await netctl.active_source(cfg)
        started_on = (started_on or "").split(":")[0].lower()
        steps: list[str] = []

        old_ip = None
        if started_on in doomed:
            old_ip = await public_ip()

        # Park the route on a survivor before the box goes down. remember=False
        # so this temporary hop never becomes the sticky preference.
        moved = False
        survivor = started_on if started_on not in doomed else ""
        if started_on in doomed or started_on not in ("wifi", "eth", "sms"):
            survivor, why = await _pick_survivor(cfg, target)
            if survivor is None:
                return {"ok": False, "error": f"cannot reboot {target}: no "
                                              f"surviving path is up ({why})",
                        "steps": steps}
            steps.append(f"switch -> {survivor}")
            sw = await netctl.switch(cfg, survivor, remember=False)
            if not sw.get("ok"):
                return {"ok": False, "error": f"cannot reboot {target}: failed to "
                                              f"move onto {survivor} "
                                              f"({sw.get('output') or sw.get('error')})",
                        "steps": steps}
            moved = True
            if not await netctl.has_internet(cfg.net_check_host, cfg.net_check_port,
                                             cfg.net_check_timeout):
                await asyncio.sleep(4.0)
                if not await netctl.has_internet(cfg.net_check_host,
                                                 cfg.net_check_port,
                                                 cfg.net_check_timeout):
                    steps.append(f"abort: {survivor} has no internet")
                    if restore and started_on in ("wifi", "eth", "sms"):
                        await netctl.switch(cfg, started_on, remember=False)
                    return {"ok": False, "error": f"cannot reboot {target}: "
                                                  f"{survivor} has no internet — "
                                                  f"refusing to cut the last uplink",
                            "steps": steps}
            steps.append(f"{survivor} online")

        # Hold off the watchdog / warm check for the whole outage window.
        _suppress(wait_s + 120.0, target)
        try:
            steps.append(f"reboot {target}")
            fired = await _TRIGGERS[target](cfg)
            if not fired.get("ok"):
                if moved and restore and started_on in ("wifi", "eth", "sms"):
                    await netctl.switch(cfg, started_on, remember=False)
                return {"ok": False, "error": fired.get("error"), "steps": steps}

            _last_reboot[target] = time.monotonic()
            await asyncio.sleep(10.0)
            back = await _wait_back(cfg, target, wait_s)
            steps.append(f"{target} back on LAN" if back
                         else f"{target} still down after {int(wait_s)}s")

            new_ip = None
            if restore and started_on in doomed and back:
                steps.append(f"switch back -> {started_on}")
                rs = await netctl.switch(cfg, started_on, remember=False)
                if rs.get("ok"):
                    await asyncio.sleep(5.0)
                    new_ip = await public_ip()
                else:
                    steps.append(f"could not return to {started_on}; "
                                 f"staying on {survivor}")
            elif started_on not in doomed:
                new_ip = await public_ip()

            changed = bool(old_ip and new_ip and old_ip != new_ip)
            return {
                "ok": True,
                "target": target,
                "rebooted": True,
                "came_back": back,
                "old_public_ip": old_ip,
                "new_public_ip": new_ip,
                "ip_changed": changed,
                "parked_on": survivor if moved else started_on,
                "active": (await netctl.active_source(cfg))[0],
                "steps": steps,
            }
        finally:
            _release()


async def status(cfg: Config) -> dict:
    """Reachability of all boxes plus the current public IP."""
    dsl_up, eth_up, sms_up, ip, active = await asyncio.gather(
        _lan_alive(cfg.router_dsl_host),
        _lan_alive(cfg.router_eth_host) if cfg.router_eth_host
        else asyncio.sleep(0, result=None),
        _lan_alive(cfg.router_sms_host),
        public_ip(),
        netctl.active_source(cfg),
    )
    now = time.monotonic()

    def _box(name: str, host: str, reachable) -> dict:
        return {"host": host or None, "reachable": reachable,
                "serves": list(BOX_PATHS[name]),
                "secs_since_reboot": (int(now - _last_reboot[name])
                                      if name in _last_reboot else None)}

    return {
        "ok": True,
        "public_ip": ip,
        "active": active[0],
        "local_ip": active[1],
        "reboot_in_progress": reboot_in_progress() or None,
        "boxes": {
            "dsl": _box("dsl", cfg.router_dsl_host, dsl_up),
            "eth": _box("eth", cfg.router_eth_host, eth_up),
            "sms": _box("sms", cfg.router_sms_host, sms_up),
        },
    }
