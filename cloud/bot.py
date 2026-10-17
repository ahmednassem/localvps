"""bot.py — admin Telegram bot for the local-server gate.

Single-admin control surface. Every command is rejected unless it comes from
ADMIN_USER_ID. Commands translate to hub.send_command() (control tunnel) or to
direct smart-plug calls.

Two devices: most commands take an optional device number FIRST
(device 1 or device 2) and default to device 1:

  /status            → all devices
  /net_sms 2         → switch device 2 to the SMS box
  /plug_cycle 2      → power-cycle device 2's plug
  /offline           → shut down device 1

/plug2* commands still work as aliases for device 2's plug.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from hub import Hub
from netrotate import LABEL as PATH_LABEL
from netrotate import PATHS
from services import DeviceServices

log = logging.getLogger("bot")

HELP_TEXT = """
<b>Local Server Bot — Commands</b>

Most commands take an optional device number first (1 or 2, default 1):
<i>/net_sms 2, /plug_cycle 2, /offline 2 …</i>

<b>Status</b>
/status — all devices: online, CPU, RAM, net, paths

<b>AnyDesk / RustDesk</b>
/anydesk [d] — running? + ID   (/anydesk_start /anydesk_stop /anydesk_restart)
/rustdesk [d] — running? + ID  (/rustdesk_start /rustdesk_stop /rustdesk_restart)

<b>Power (device)</b>
/offline [d] — shut down device (OS shutdown, plug stays on)
/online [d] — wake device (plug off → on)
/open [d] — same as /online
/reboot [d] — restart device
/shutdown [d] — shut down (no offline lock)
/stay_online [d] on|off — keep on ALL night / re-arm night idle shutdown

<b>Internet</b>
/net_wifi [d] — switch to main DSL Wi‑Fi
/net_eth [d] — switch to the Ethernet router
/net_sms [d] — switch to the SMS box (hotspot, USB fallback)

<b>IP rotation</b>
/netrotate [d] — rotation status (path, stint, rejects, counters)
/netrotate [d] on|off — enable / disable automatic rotation
/netrotate [d] now [wifi|eth|sms] — rotate as soon as the worker is idle
/router_status [d] — all boxes reachable? + current public IP
/router_reboot [d] — reboot the DSL router (device parks on a survivor first)
/eth_reboot [d] — reboot the Ethernet router
/sms_reboot [d] — reboot the SMS box

<b>SMS dongle / adapters</b>
/sms [d] — SMS box status    /sms_on [d]   /sms_off [d]
/eth [d] — Ethernet adapter link status

<b>Smart plugs (sockets)</b>
/plug [d] — plug status (watts, on/off)
/plug_on [d]   /plug_off [d]   /plug_cycle [d]
/plug2 /plug2_on /plug2_off /plug2_cycle — aliases for device 2's plug

<b>Smart plug (lamp — on plug 1)</b>
/lamp_on   /lamp_off   (on/off only, no brightness)

<b>Processes</b>
/ps [d] — running processes
/kill [d] &lt;pid|name&gt; — kill process
/run [d] &lt;cmd&gt; — run shell command

<b>Other</b>
/actions [d] — list core actions
/help — this message
""".strip()


class GateBot:
    def __init__(self, token: str, admin_id: int, hub: Hub,
                 services: dict[int, DeviceServices]) -> None:
        self._admin = admin_id
        self._hub = hub
        self._services = services
        self._last_event_msg = ""
        self._last_event_at = 0.0
        self._app = Application.builder().token(token).build()
        self._setup()

    @property
    def app(self) -> Application:
        return self._app

    def _svc(self, device: int) -> DeviceServices:
        return self._services[device]

    def _label(self, device: int) -> str:
        return self._hub.device(device).label

    def _parse_device(self, args: list[str]) -> tuple[int, list[str]]:
        """Optional leading device number: '/cmd 2 rest…' → (2, [rest…])."""
        if args:
            try:
                device = int(args[0])
            except ValueError:
                return 1, list(args)
            if device in self._services:
                return device, list(args[1:])
        return 1, list(args)

    async def notify_admin(self, text: str, *,
                           parse_mode: str | None = None) -> None:
        try:
            await self._app.bot.send_message(
                chat_id=self._admin, text=text, parse_mode=parse_mode)
        except Exception as exc:                    # noqa: BLE001
            log.warning("notify_admin failed: %s", exc)

    async def notify_event(self, device: int, message: str,
                           level: str = "info") -> None:
        """Forward agent WS events to Telegram (AC cut, failover, etc.)."""
        msg = (message or "").strip()
        if not msg:
            return
        try:
            msg = f"[{self._label(device)}] {msg}"
        except KeyError:
            pass
        now = time.time()
        if msg == self._last_event_msg and now - self._last_event_at < 45:
            return
        self._last_event_msg = msg
        self._last_event_at = now
        await self.notify_admin(msg)

    async def send_status_digest(self) -> None:
        """Push a /status-style summary to the admin (scheduled reports)."""
        text = "📊 <b>Scheduled status</b>\n\n" + self._all_status_text()
        await self.notify_admin(text, parse_mode=ParseMode.HTML)

    def _all_status_text(self) -> str:
        parts = []
        for snap in self._hub.all_snapshots():
            text = _fmt_status(snap)
            device = snap["device"]
            health = snap.get("health") or {}
            if health.get("case") is not None:
                text += (f"\nHealth: case {health['case']} · {health.get('text', '')}"
                         + (" · power rules on hold" if health.get("hold") else ""))
            lan = (snap.get("home") or {}).get("plugs") or {}
            if snap.get("online") and lan:
                text += "\nPlugs seen at home: " + ", ".join(
                    f"{n} {'on' if p.get('socket') else 'off'} {p.get('watts', 0)} W"
                    if p.get("ok") else f"{n} not answering"
                    for n, p in sorted(lan.items()))
            node = snap.get("node") or {}
            if node.get("state"):
                text += f"\nNode: {node['state']}" + (
                    " · seen by the other device" if node.get("peer_seen") else "")
            link = snap.get("peer") or {}
            if snap.get("online") and link.get("enabled"):
                if link.get("connected"):
                    text += (f"\nPeer link: up to device {link.get('peer_device')} "
                             f"at {link.get('host')} · {link.get('rtt_ms')} ms")
                else:
                    text += f"\nPeer link: down · {link.get('error') or '?'}"
            ver = (snap.get("telemetry") or {}).get("version")
            if snap.get("online") and ver:
                text += f"\nVersion: {ver}"
            gl = (snap.get("telemetry") or {}).get("link") or {}
            if snap.get("online") and gl.get("link"):
                text += f"\nLink: {gl['link']} ({gl.get('host') or '?'})" + (
                    f" · on the IP {gl['on_ip_s'] // 60} min" if gl.get("on_ip_s") else "")
            svc = self._services.get(device)
            if svc is not None and snap.get("online"):
                r = svc.netrotate.snapshot()
                cur = PATH_LABEL.get(r.get("current"), r.get("current"))
                text += (f"\nRotation: {'on' if r.get('enabled') else 'off'} · "
                         f"{cur} · stint {r.get('stint')} · streak "
                         f"{r.get('reject_streak')}"
                         + (" · ⏳ pending" if r.get("pending_rotation") else "")
                         + f" · switches {r.get('switches')}"
                         + (f" · box reboot in {r['box_reboot_in']}"
                            if r.get("box_reboot_in") and any((r.get("box_reboots") or {}).values())
                            else " · box reboots off")
                         + (f" · SMS bad {r['sms_bad_s'] // 60 + 1} min"
                            if r.get("sms_bad_s") else ""))
            parts.append(text)
        health = getattr(self._hub, "health", None)
        if health is not None:
            parts.append("\n".join(["<b>Plugs</b>", *health.plug_lines(),
                                    health.net.status_line()]))
        cfw = getattr(self._hub, "cfwatch", None)
        if cfw is not None:
            parts.append(cfw.status_line())
        return "\n\n".join(parts)

    def _setup(self) -> None:
        add = self._app.add_handler

        # help + status
        add(CommandHandler("start", self._help))
        add(CommandHandler("help", self._help))
        add(CommandHandler("status", self._status))

        # anydesk
        self._cmd("anydesk", "action", "anydesk", {"op": "status"})
        self._cmd("anydesk_start", "action", "anydesk", {"op": "start"})
        self._cmd("anydesk_stop", "action", "anydesk", {"op": "stop"})
        self._cmd("anydesk_restart", "action", "anydesk", {"op": "restart"})

        # rustdesk (self-hosted)
        self._cmd("rustdesk", "action", "rustdesk", {"op": "status"})
        self._cmd("rustdesk_start", "action", "rustdesk", {"op": "start"})
        self._cmd("rustdesk_stop", "action", "rustdesk", {"op": "stop"})
        self._cmd("rustdesk_restart", "action", "rustdesk", {"op": "restart"})

        # device power / intentional offline
        add(CommandHandler("offline", self._offline))
        add(CommandHandler("online", self._online))
        add(CommandHandler("open", self._online))
        add(CommandHandler("stay_online", self._stay_online))
        add(CommandHandler("stayonline", self._stay_online))
        self._cmd("reboot", "action", "reboot")
        self._cmd("shutdown", "action", "shutdown")

        # internet
        self._cmd("net_eth", "action", "net", {"source": "eth"}, timeout=90.0)
        self._cmd("net_wifi", "action", "net", {"source": "wifi"}, timeout=90.0)
        self._cmd("net_sms", "action", "net", {"source": "sms"}, timeout=90.0)

        # IP rotation + router reboots
        add(CommandHandler("netrotate", self._netrotate_cmd))
        add(CommandHandler("router_status", self._router_status))
        add(CommandHandler("router_reboot", self._router_reboot("dsl")))
        add(CommandHandler("eth_reboot", self._router_reboot("eth")))
        add(CommandHandler("sms_reboot", self._router_reboot("sms")))

        # sms dongle
        self._cmd("sms", "action", "device", {"device": "sms", "op": "status"})
        self._cmd("sms_on", "action", "device", {"device": "sms", "op": "on"})
        self._cmd("sms_off", "action", "device", {"device": "sms", "op": "off"})

        # eth adapter status
        self._cmd("eth", "action", "device", {"device": "eth", "op": "status"})

        # processes
        self._cmd("ps", "action", "ps")
        self._cmd("actions", "action", "actions")
        add(CommandHandler("run", self._run))
        add(CommandHandler("kill", self._kill))

        # smart plugs — dedicated commands (+ plug2 aliases for device 2)
        add(CommandHandler("plug", self._plug_status_cmd()))
        add(CommandHandler("plug_on", self._plug_op("on")))
        add(CommandHandler("plug_off", self._plug_op("off")))
        add(CommandHandler("plug_cycle", self._plug_op("cycle")))
        add(CommandHandler("plug2", self._plug_status_cmd(force_device=2)))
        add(CommandHandler("plug2_on", self._plug_op("on", force_device=2)))
        add(CommandHandler("plug2_off", self._plug_op("off", force_device=2)))
        add(CommandHandler("plug2_cycle", self._plug_op("cycle", force_device=2)))
        add(CommandHandler("lamp_on", self._lamp_on))
        add(CommandHandler("lamp_off", self._lamp_off))
        add(CommandHandler("lamp_bright", self._lamp_bright))

        # legacy arg-style (backward compatible)
        add(CommandHandler("net", self._net_legacy))
        add(CommandHandler("device", self._device_legacy))
        add(CommandHandler("power", self._power_legacy))
        add(CommandHandler("lamp", self._lamp_legacy))

    def _cmd(self, command: str, kind: str, name: str,
             args: dict | None = None, *, timeout: float = 45.0) -> None:
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._ok(update):
                return await self._deny(update)
            device, _rest = self._parse_device(ctx.args or [])
            await self._do(update, device, kind, name, args or {},
                           timeout=timeout)
        self._app.add_handler(CommandHandler(command, handler))

    def _plug_status_cmd(self, force_device: int | None = None) -> Callable:
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._ok(update):
                return await self._deny(update)
            device = force_device or self._parse_device(ctx.args or [])[0]
            await self._plug_status(update, device)
        return handler

    def _plug_op(self, op: str, force_device: int | None = None) -> Callable:
        async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._ok(update):
                return await self._deny(update)
            device = force_device or self._parse_device(ctx.args or [])[0]
            await self._plug_action(update, device, op)
        return handler

    def _ok(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u and u.id == self._admin)

    async def _deny(self, update: Update) -> None:
        await update.effective_message.reply_text("unauthorized")

    async def _do(self, update, device: int, kind: str, name: str,
                  args: dict | None = None, timeout: float = 45.0) -> None:
        try:
            res = await self._hub.send_command(device, kind, name, args or {},
                                               timeout=timeout)
        except RuntimeError:
            await update.effective_message.reply_text(
                f"{self._label(device)}: core offline")
            return
        except TimeoutError:
            await update.effective_message.reply_text(
                f"{self._label(device)}: timeout waiting for core")
            return
        await update.effective_message.reply_text(_fmt_result(res))

    async def _help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_message.reply_text(
            HELP_TEXT, parse_mode=ParseMode.HTML)

    async def _status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_message.reply_text(
            self._all_status_text(), parse_mode=ParseMode.HTML)

    async def _offline(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, _rest = self._parse_device(ctx.args or [])
        res = await self._svc(device).vps.offline()
        await update.effective_message.reply_text(
            f"[{self._label(device)}] " + _fmt_vps_result("offline", res))

    async def _online(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, _rest = self._parse_device(ctx.args or [])
        res = await self._svc(device).vps.online(notify=False)
        await update.effective_message.reply_text(
            f"[{self._label(device)}] " + _fmt_vps_result("online", res))

    async def _stay_online(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        arg = (rest[0].lower() if rest else "on")
        if arg not in ("on", "off"):
            return await update.effective_message.reply_text(
                "usage: /stay_online [device] on  |  /stay_online [device] off")
        night = self._svc(device).night
        snap = night.set_stay_online(arg == "on")
        label = self._label(device)
        if arg == "on":
            in_night = snap.get("night")
            when = "now (night window open)" if in_night else "for the coming night"
            return await update.effective_message.reply_text(
                f"🌙 [{label}] Stay-online ON {when} — the PC will NOT be shut "
                "down for night idle, even with no tasks. Resets automatically "
                "when the night window ends, or use /stay_online off.")
        return await update.effective_message.reply_text(
            f"🌙 [{label}] Stay-online OFF — night idle shutdown re-armed "
            "(timer restarted fresh).")

    async def _plug_status(self, update: Update, device: int) -> None:
        plug = self._svc(device).plug
        label = f"plug {device} ({self._label(device)})"
        if not plug.configured:
            return await update.effective_message.reply_text(
                f"{label} not configured")
        res = await plug.status()
        await update.effective_message.reply_text(
            _fmt_plug_status(res, label=label))

    async def _plug_action(self, update: Update, device: int, op: str) -> None:
        plug = self._svc(device).plug
        label = f"plug {device} ({self._label(device)})"
        if not plug.configured:
            await update.effective_message.reply_text(
                f"{label} not configured")
            return
        res = await getattr(plug, op)()
        await update.effective_message.reply_text(
            f"{label}: {json.dumps(res)}")

    # lamp lives on plug 1
    async def _lamp_on(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        plug = self._svc(1).plug
        if not plug.configured:
            return await update.effective_message.reply_text("smart plug not configured")
        res = await plug.lamp(True)
        await update.effective_message.reply_text(json.dumps(res))

    async def _lamp_off(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        plug = self._svc(1).plug
        if not plug.configured:
            return await update.effective_message.reply_text("smart plug not configured")
        res = await plug.lamp(False)
        await update.effective_message.reply_text(json.dumps(res))

    async def _lamp_bright(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        plug = self._svc(1).plug
        if not plug.configured:
            return await update.effective_message.reply_text("smart plug not configured")
        if not ctx.args:
            return await update.effective_message.reply_text(
                "usage: /lamp_bright <0-100>\nexample: /lamp_bright 50")
        try:
            pct = int(ctx.args[0])
        except ValueError:
            return await update.effective_message.reply_text("brightness must be a number 0-100")
        res = await plug.brightness(pct)
        await update.effective_message.reply_text(json.dumps(res))

    # ── IP rotation / router reboots ────────────────────────────────────────
    async def _netrotate_cmd(self, update: Update,
                             ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        nr = self._svc(device).netrotate
        arg = (rest[0].lower() if rest else "status")
        if arg in ("on", "off"):
            snap = nr.set_enabled(arg == "on")
            return await update.effective_message.reply_text(
                f"rotation {'ENABLED' if snap['enabled'] else 'DISABLED'}\n"
                + _fmt_rotate(snap, self._label(device)),
                parse_mode=ParseMode.HTML)
        if arg == "now":
            to = (rest[1].lower() if len(rest) > 1 else "")
            await update.effective_message.reply_text(
                "rotating as soon as the worker is idle…")
            snap = await nr.rotate_now(to=to, reason="manual (/netrotate now)")
            return await update.effective_message.reply_text(
                _fmt_rotate(snap, self._label(device)),
                parse_mode=ParseMode.HTML)
        if arg != "status":
            return await update.effective_message.reply_text(
                "usage: /netrotate [device] [status|on|off|now [wifi|eth|sms]]")
        await update.effective_message.reply_text(
            _fmt_rotate(nr.snapshot(), self._label(device)),
            parse_mode=ParseMode.HTML)

    async def _router_status(self, update: Update,
                             ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, _rest = self._parse_device(ctx.args or [])
        try:
            res = await self._hub.send_command(
                device, "action", "router", {"op": "status"}, timeout=45.0)
        except RuntimeError:
            return await update.effective_message.reply_text(
                f"{self._label(device)}: core offline")
        except TimeoutError:
            return await update.effective_message.reply_text(
                f"{self._label(device)}: timeout waiting for core")
        await update.effective_message.reply_text(_fmt_router_status(res))

    def _router_reboot(self, target: str) -> Callable:
        name = {"dsl": "DSL router", "eth": "Ethernet router",
                "sms": "SMS box"}[target]

        async def handler(update: Update,
                          ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._ok(update):
                return await self._deny(update)
            device, rest = self._parse_device(ctx.args or [])
            force = bool(rest and rest[0].lower() == "force")
            await update.effective_message.reply_text(
                f"rebooting the {name} — {self._label(device)} parks on a "
                "surviving path first, then comes back. ~2-3 min…")
            res = await self._svc(device).netrotate.reboot_box(
                target, why=f"manual (/{target}_reboot)"
                if target != "dsl" else "manual (/router_reboot)",
                force=force)
            # reboot_box already sent the outcome to the admin
            if not res.get("ok"):
                await update.effective_message.reply_text(
                    f"failed: {res.get('error', '?')}")
        return handler

    async def _run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        cmd = " ".join(rest)
        if not cmd:
            return await update.effective_message.reply_text(
                "usage: /run [device] <command>\nexample: /run ipconfig")
        await self._do(update, device, "shell", "shell", {"cmd": cmd},
                       timeout=120.0)

    async def _kill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        if not rest:
            return await update.effective_message.reply_text(
                "usage: /kill [device] <pid|name>\nexample: /kill notepad")
        await self._do(update, device, "action", "kill", {"target": rest[0]})

    # ── legacy arg commands ─────────────────────────────────────────────────
    async def _net_legacy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        if not rest:
            return await update.effective_message.reply_text(
                "use /net_eth /net_wifi /net_sms")
        src = rest[0].lower()
        if src not in ("eth", "wifi", "sms"):
            return await update.effective_message.reply_text(
                "use /net_eth /net_wifi /net_sms")
        await self._do(update, device, "action", "net", {"source": src},
                       timeout=90.0)

    async def _device_legacy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        if not rest:
            return await update.effective_message.reply_text(
                "use /sms /sms_on /sms_off /eth")
        name = rest[0].lower()
        op = rest[1].lower() if len(rest) > 1 else "status"
        await self._do(update, device, "action", "device",
                       {"device": name, "op": op})

    async def _power_legacy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        device, rest = self._parse_device(ctx.args or [])
        op = (rest[0].lower() if rest else "")
        hint = "use /plug /plug_on /plug_off /plug_cycle"
        if op not in ("on", "off", "cycle", "status"):
            return await update.effective_message.reply_text(hint)
        if op == "status":
            return await self._plug_status(update, device)
        await self._plug_action(update, device, op)

    async def _lamp_legacy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        if not ctx.args:
            return await update.effective_message.reply_text(
                "use /lamp_on /lamp_off /lamp_bright <pct>")
        op = ctx.args[0].lower()
        if op == "on":
            return await self._lamp_on(update, ctx)
        if op == "off":
            return await self._lamp_off(update, ctx)
        if op in ("bright", "brightness"):
            ctx.args.pop(0)
            return await self._lamp_bright(update, ctx)
        return await update.effective_message.reply_text(
            "use /lamp_on /lamp_off /lamp_bright <pct>")


# ── formatting ──────────────────────────────────────────────────────────────
def _fmt_result(res: dict) -> str:
    if not isinstance(res, dict):
        return str(res)
    ok = res.get("ok")
    out = res.get("output")
    parts = [f"ok={ok}"]
    if res.get("code") is not None:
        parts.append(f"code={res['code']}")
    head = " ".join(parts)
    if out:
        text = out if isinstance(out, str) else json.dumps(out, indent=2)
        if len(text) > 3500:
            text = text[:3500] + "\n… (truncated)"
        return f"{head}\n{text}"
    if res.get("error"):
        return f"{head}\nerror: {res['error']}"
    skip = {"ok", "type", "id", "output", "code", "error"}
    extras = {k: v for k, v in res.items() if k not in skip and v is not None}
    if extras:
        text = json.dumps(extras, indent=2, default=str)
        if len(text) > 3500:
            text = text[:3500] + "\n… (truncated)"
        return f"{head}\n{text}"
    return head


def _fmt_vps_result(op: str, res: dict) -> str:
    if res.get("ok"):
        if op == "offline":
            sk = res.get("shutdown")
            if sk == "skipped":
                return (
                    "ok — device OFFLINE (already down). "
                    "Use /online to boot."
                )
            return (
                "ok — device shutting down. "
                "Stays off until /online or scheduled open."
            )
        return "ok — wake cycle started (plug off → on). Wait ~2 min then /status."
    return f"failed: {res.get('error', '?')}"


def _fmt_plug_status(h: dict, label: str = "plug") -> str:
    if not h.get("ok"):
        return f"{label} error: {h.get('error', '?')}"
    if not h.get("online"):
        return f"{label}: OFFLINE"
    via = {"mqtt": "the VPS (MQTT)", "lan": "a laptop on the LAN"}.get(h.get("path"), "")
    lines = [
        f"{label}: ONLINE" + (f" · read via {via}" if via else ""),
        f"socket: {'ON' if h.get('socket') else 'OFF'}",
        f"draw: {h.get('watts')} W @ {h.get('volts')} V"
        + ("  <- device running" if h.get("laptop_running") else "  <- idle"),
    ]
    if h.get("lamp") is not None:
        lines.append(f"lamp: {'ON' if h.get('lamp') else 'OFF'}")
    if h.get("kwh") is not None:
        lines.append(f"total: {h.get('kwh')} kWh")
    if h.get("wifi"):
        wifi = {"main": "main router", "backup": "SMS box (backup)"}.get(h["wifi"], h["wifi"])
        sig = f" · {h['signal']} dBm" if h.get("signal") is not None else ""
        lines.append(f"Wi-Fi: {wifi}{sig}")
    return "\n".join(lines)


def _fmt_status(snap: dict) -> str:
    label = snap.get("label", f"device {snap.get('device')}")
    if snap.get("intentional_offline"):
        secs = snap.get("seconds_since_status")
        extra = f" ({int(secs)}s since last status)" if isinstance(secs, (int, float)) else ""
        return (
            f"🔴 <b>{label} — OFF (manual shutdown)</b>{extra}\n"
            "Device shut down — plug still on.\n"
            "Use /online to boot, or wait for scheduled open."
        )
    if not snap.get("online"):
        secs = snap.get("seconds_since_status")
        extra = f" ({int(secs)}s since last status)" if isinstance(secs, (int, float)) else ""
        return f"🔴 <b>{label} — OFFLINE</b>{extra}"

    t = snap.get("telemetry", {}) or {}
    dev = snap.get("devices") or t.get("devices") or {}
    if not isinstance(dev, dict):
        dev = {}
    ad = t.get("anydesk") or {}
    rd = t.get("rustdesk") or {}

    plugged = snap.get("plugged")
    if plugged is None:
        plugged = t.get("plugged")
    if plugged is None:
        plugged = t.get("ac_connected")
    internet = snap.get("internet")
    if internet is None:
        internet = t.get("internet")

    if internet and plugged:
        badge = "🟢"
    elif plugged is False:
        badge = "🟠"
    else:
        badge = "🟡"

    power = snap.get("power") or t.get("power") or _power_line(t, plugged)
    batt = snap.get("battery_pct")
    if batt is None:
        batt = t.get("battery_pct", t.get("battery"))
    charging = snap.get("charging")
    if charging is None:
        charging = t.get("charging")

    net = snap.get("net") or t.get("net", "?")
    ip = snap.get("ip") or t.get("ip", "")

    lines = [f"<b>{badge} {label} — ONLINE</b>"]
    if plugged is False:
        lines.append("<b>⚠️ ON BATTERY</b>")
    lines.extend([
        f"Host: {snap.get('host', '?')}",
        f"Net: {net}  IP: {ip}  Internet: {_yes_no(internet)}",
        f"Power: {power}",
        f"  battery={batt}%  AC={_yes_no(plugged)}  charging={_yes_no(charging)}",
        "Paths:",
        f"  wifi: {dev.get('wifi', 'absent')}",
        f"  eth:  {dev.get('eth', 'absent')}",
        f"  sms:  {dev.get('sms', 'absent')}",
    ])

    cpu = t.get("cpu")
    ram = t.get("ram")
    disk = t.get("disk")
    temp = t.get("temp")
    if any(v is not None for v in (cpu, ram, disk, temp)):
        lines.append(
            f"CPU/RAM/Disk/Temp: {cpu}% / {ram}% / {disk}% / {temp}°C"
        )

    ad_run = ad.get("running")
    ad_elev = ad.get("elevated")
    ad_id = ad.get("id")
    ad_parts = ["running" if ad_run else "stopped"]
    if ad_elev:
        ad_parts.append("elevated")
    if ad_id:
        ad_parts.append(f"id={ad_id}")
    lines.append(f"AnyDesk: {' '.join(ad_parts)}")

    rd_parts = ["running" if rd.get("running") else "stopped"]
    if rd.get("elevated"):
        rd_parts.append("elevated")
    if rd.get("id"):
        rd_parts.append(f"id={rd.get('id')}")
    lines.append(f"RustDesk: {' '.join(rd_parts)}")
    lines.append(f"Commands: {snap.get('command_channel', '?')}")
    return "\n".join(str(x) for x in lines)


def _fmt_rotate(s: dict, device_label: str = "") -> str:
    if not s or not s.get("ok"):
        return "rotation: n/a"
    cur = s.get("current", "?")
    label = PATH_LABEL.get(cur, cur)
    head = f"<b>🔁 IP rotation — {'ON' if s.get('enabled') else 'OFF'}</b>"
    if device_label:
        head = f"<b>🔁 [{device_label}] IP rotation — " \
               f"{'ON' if s.get('enabled') else 'OFF'}</b>"
    lines = [
        head,
        f"Now on: {label}   stint {s.get('stint')}   reject streak {s.get('reject_streak')}",
        f"In flight: {s.get('inflight')}   rotations: {s.get('rotations')}",
    ]
    if s.get("pending_rotation"):
        lines.append(f"⏳ rotation pending ({s.get('pending_for_s')}s): "
                     f"{s.get('pending_reason')}")
    if s.get("cooldown_left_s"):
        lines.append(f"cooldown: {s['cooldown_left_s']}s")
    paths = s.get("paths") or {}
    for p in PATHS:
        st = paths.get(p) or {}
        if not st:
            continue
        att = st.get("attempts", 0)
        rate = (st.get("done", 0) / att * 100) if att else 0
        lines.append(
            f"  {p}: {att} attempts · done {st.get('done', 0)} · rejected "
            f"{st.get('rejected', 0)} · auth {st.get('auth_error', 0)} "
            f"· failed {st.get('failed', 0)} → {rate:.0f}% ok")
    dead = s.get("dead_penalty") or {}
    for p, left in dead.items():
        lines.append(f"  ⚠ {p} marked dead — retry after {left} more attempts")
    lr = s.get("last_reboot") or {}
    for box, info in lr.items():
        ago = int(time.time() - info.get("at", time.time()))
        ipl = ""
        if info.get("old_ip") or info.get("new_ip"):
            ipl = f" {info.get('old_ip')} → {info.get('new_ip')}"
        lines.append(f"  last {box} reboot: {ago // 60} min ago, "
                     f"{'ok' if info.get('ok') else 'FAILED'}{ipl}")
    cfg = s.get("config") or {}
    lines.append(
        f"rules: every {cfg.get('every_tasks')} attempts · early after "
        f"{cfg.get('reject_streak')} rejects · report every "
        f"{cfg.get('report_every')} · dead penalty {cfg.get('dead_penalty_tasks')} "
        f"· reboot DSL on leave: {_yes_no(cfg.get('reboot_dsl_on_leave'))}")
    return "\n".join(lines)


def _fmt_router_status(res: dict) -> str:
    if not isinstance(res, dict) or not res.get("ok"):
        return f"router status failed: {res.get('error', res) if isinstance(res, dict) else res}"
    boxes = res.get("boxes") or {}
    lines = [
        f"public IP: {res.get('public_ip')}   device on: {res.get('active')} "
        f"({res.get('local_ip')})",
    ]
    if res.get("reboot_in_progress"):
        lines.append(f"⏳ {res['reboot_in_progress']} reboot in progress")
    for name, b in boxes.items():
        since = b.get("secs_since_reboot")
        ago = f", rebooted {since // 60} min ago" if isinstance(since, int) else ""
        lines.append(
            f"  {name} ({b.get('host')}): "
            f"{'reachable' if b.get('reachable') else 'UNREACHABLE'}"
            f" — serves {', '.join(b.get('serves') or [])}{ago}")
    return "\n".join(lines)


def _yes_no(val) -> str:
    if val is True:
        return "yes"
    if val is False:
        return "no"
    return "?"


def _power_line(t: dict, plugged) -> str:
    pct = t.get("battery_pct", t.get("battery"))
    if pct is None:
        return "unknown"
    if plugged:
        return f"AC ({pct}%)"
    if plugged is False:
        return f"BATTERY ({pct}%)"
    return f"{pct}%"
