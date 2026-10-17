"""actions.py — the command registry executed on the laptop.

The agent calls dispatch() for control-tunnel commands. Actions are async and
return a JSON-able dict (usually {"ok":bool,"output":str,...}). New services can
register their own actions with the @action decorator so the VPS can drive
them without changing the core.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import psutil

import anydesk_mgr
import devices
import rustdesk_mgr
import netctl
import plugs
import session
from config import Config
from winutil import run

log = logging.getLogger("actions")

ACTIONS: dict[str, Callable[["ActionContext", dict], Awaitable[dict]]] = {}


def action(name: str):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


class ActionContext:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg


async def dispatch(kind: str, name: str, args: dict, ctx: "ActionContext") -> dict:
    if kind == "shell":
        return await _shell(ctx, args)
    fn = ACTIONS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown action: {name}"}
    try:
        return await fn(ctx, args)
    except Exception as exc:                          # noqa: BLE001
        log.exception("action %s failed", name)
        return {"ok": False, "error": str(exc)}


# ── built-ins ──────────────────────────────────────────────────────────────
async def _shell(ctx: ActionContext, args: dict) -> dict:
    cmd = args.get("cmd", "")
    if not cmd:
        return {"ok": False, "error": "cmd required"}
    shell = args.get("shell", "powershell")
    code, out = await run(cmd, timeout=float(args.get("timeout", 110)), shell=shell)
    return {"ok": code == 0, "code": code, "output": out}


@action("actions")
async def _actions(ctx: ActionContext, args: dict) -> dict:
    return {"ok": True, "output": ", ".join(sorted(ACTIONS.keys()))}


@action("anydesk")
async def _anydesk(ctx: ActionContext, args: dict) -> dict:
    op = (args.get("op") or "start").lower()
    if op == "status":
        return await anydesk_mgr.status(ctx.cfg)
    if op == "stop":
        return await anydesk_mgr.stop()
    if op == "restart":
        return await anydesk_mgr.restart(ctx.cfg)
    if op == "start":
        return await anydesk_mgr.start(ctx.cfg)
    return {"ok": False, "error": "op must be start|stop|restart|status"}


@action("rustdesk")
async def _rustdesk(ctx: ActionContext, args: dict) -> dict:
    op = (args.get("op") or "start").lower()
    if op == "status":
        return await rustdesk_mgr.status(ctx.cfg)
    if op == "stop":
        return await rustdesk_mgr.stop()
    if op == "restart":
        return await rustdesk_mgr.restart(ctx.cfg)
    if op == "start":
        return await rustdesk_mgr.start(ctx.cfg)
    return {"ok": False, "error": "op must be start|stop|restart|status"}


@action("reboot")
async def _reboot(ctx: ActionContext, args: dict) -> dict:
    code, out = await run("shutdown /r /f /t 3", timeout=10)
    return {"ok": code == 0, "code": code, "output": out or "rebooting"}


@action("shutdown")
async def _shutdown(ctx: ActionContext, args: dict) -> dict:
    code, out = await run("shutdown /s /f /t 3", timeout=10)
    return {"ok": code == 0, "code": code, "output": out or "shutting down"}


@action("net")
async def _net(ctx: ActionContext, args: dict) -> dict:
    source = args.get("source", "")
    if not source:
        src, ip = await netctl.active_source(ctx.cfg)
        label = {"wifi": "main Wi-Fi (DSL)", "eth": "Ethernet",
                 "sms": "SMS backup"}.get(
            (src or "").split(":")[0], src)
        return {"ok": True, "output": f"active: {label} / {src} ({ip})"}
    blocked = await netctl.ensure_path_up(ctx.cfg, source)
    if blocked is not None:
        return blocked
    # remember=False is sent by the gate's IP-rotation policy: those moves are
    # temporary and must not overwrite the user's sticky /net_* choice.
    remember = bool(args.get("remember", True))
    res = await netctl.switch(ctx.cfg, source, remember=remember)
    if not res.get("ok"):
        res["error"] = (
            res.get("error")
            or res.get("output")
            or f"switch to {source} failed"
        )
    else:
        # Manual pick clears auto-failover sticky; TG choice wins
        name = {"wifi": "main Wi-Fi (DSL)", "eth": "Ethernet",
                "sms": "SMS backup"}.get(source, source)
        res["output"] = res.get("output") or f"switched to {name}"
        import net_status
        res = await net_status.apply_after_switch(ctx.cfg, source, res)
    return res


@action("router")
async def _router(ctx: ActionContext, args: dict) -> dict:
    """Reboot a network box, or report both boxes and the public IP.

    op=status              both boxes + current public IP
    op=reboot target=dsl   reboot the DSL router (parks the route on SMS first)
    op=reboot target=sms   reboot the SMS box (parks the route on Wi-Fi first)
    op=ip                  just the public IP
    """
    import router_mgr

    op = (args.get("op") or "status").lower()
    if op == "status":
        return await router_mgr.status(ctx.cfg)
    if op == "ip":
        ip = await router_mgr.public_ip()
        return {"ok": bool(ip), "public_ip": ip,
                "output": ip or "could not read public IP"}
    if op == "reboot":
        target = (args.get("target") or "dsl").lower()
        res = await router_mgr.reboot(
            ctx.cfg, target,
            force=bool(args.get("force")),
            restore=bool(args.get("restore", True)),
        )
        if res.get("ok"):
            old, new = res.get("old_public_ip"), res.get("new_public_ip")
            if res.get("ip_changed"):
                ipline = f"IP {old} -> {new}"
            elif old and new:
                ipline = f"IP unchanged ({new})"
            else:
                ipline = f"IP {new or old or 'unknown'}"
            res["output"] = (f"{target} rebooted, "
                             f"{'back' if res.get('came_back') else 'STILL DOWN'}; "
                             f"{ipline}; on {res.get('active')}")
        return res
    return {"ok": False, "error": "op must be status|reboot|ip"}


@action("device")
async def _device(ctx: ActionContext, args: dict) -> dict:
    name = (args.get("device") or "").lower()
    op = (args.get("op") or "status").lower()
    pattern = {"fan": ctx.cfg.fan_device,
               "eth": ctx.cfg.eth_adapter,
               "sms": ctx.cfg.sms_device}.get(name)
    if pattern is None:
        return {"ok": False, "error": "device must be fan|eth|sms"}
    if op == "status":
        return {"ok": True, "output": await devices.status(pattern)}
    if op == "on":
        return await devices.set_enabled(pattern, True)
    if op == "off":
        return await devices.set_enabled(pattern, False)
    if op == "reset":
        await devices.set_enabled(pattern, False)
        r = await devices.set_enabled(pattern, True)
        r["output"] = "reset (off->on)"
        return r
    return {"ok": False, "error": "op must be status|on|off|reset"}


@action("plug")
async def _plug(ctx: ActionContext, args: dict) -> dict:
    """Plug 1 or 2 over the home LAN: {"plug": 2, "op": "status|on|off|cycle"}."""
    try:
        n = int(args.get("plug", ctx.cfg.device_id))
    except (TypeError, ValueError):
        return {"ok": False, "error": "plug must be 1 or 2"}
    op = str(args.get("op") or "status").lower()
    return await plugs.run(op, n, gap=float(args.get("gap", 10)))


@action("peer")
async def _peer(ctx: ActionContext, args: dict) -> dict:
    """Peer link: {"op": "status|hint|echo", "host": "192.168.1.x"}."""
    import peer
    link = peer.LINK
    if link is None:
        return {"ok": False, "error": "peer link not running"}
    op = (args.get("op") or "status").lower()
    if op == "hint":
        link.set_hint(str(args.get("host") or ""))
    elif op == "echo":
        res = await link.request("echo", {"from": ctx.cfg.device_id})
        return {**res, "output": json.dumps(res)}
    elif op != "status":
        return {"ok": False, "error": "op: status | hint | echo"}
    st = link.status()
    return {"ok": True, "peer": st, "output": json.dumps(st)}


@action("ps")
async def _ps(ctx: ActionContext, args: dict) -> dict:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(p.info)
        except Exception:                            # noqa: BLE001
            continue
    procs.sort(key=lambda x: (x.get("memory_percent") or 0), reverse=True)
    lines = ["PID     MEM%   CPU%   NAME"]
    for p in procs[:25]:
        lines.append(f"{p['pid']:<7} {(p.get('memory_percent') or 0):>4.1f}  "
                     f"{(p.get('cpu_percent') or 0):>5.1f}  {p.get('name')}")

    listening = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr:
                listening.append(f"{c.laddr.ip}:{c.laddr.port} (pid {c.pid})")
    except Exception:                                # noqa: BLE001
        pass
    lines.append("")
    lines.append("listening: " + (", ".join(sorted(set(listening))[:30]) or "none"))
    return {"ok": True, "output": "\n".join(lines)}


@action("kill")
async def _kill(ctx: ActionContext, args: dict) -> dict:
    target = str(args.get("target", "")).strip()
    if not target:
        return {"ok": False, "error": "target (pid or name) required"}
    killed = []
    if target.isdigit():
        pid = int(target)
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": str(exc)}
    else:
        tl = target.lower()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if tl in (p.info["name"] or "").lower():
                    p.kill()
                    killed.append(p.info["pid"])
            except Exception:                        # noqa: BLE001
                continue
    return {"ok": bool(killed), "output": f"killed: {killed}"}
