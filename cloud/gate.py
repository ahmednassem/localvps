"""gate.py — HTTP/WebSocket frontend of the local-server gate on the VPS.

Devices (device 1 or device 2) connect UP to this gate on the
server's direct IP; admin/output consumers talk DOWN over loopback.

  POST /status                       device heartbeat (X-Agent-Token, ~60s,
                                     body carries "device": 1|2)
  WS  /agent/ws                      command channel — hello carries device id

  --- admin API (X-Admin-Key: ADMIN_KEY) -----------------------------------
  GET  /api/status                   all devices: online + last telemetry
  GET  /api/{device}/status          one device
  GET  /api/cluster                  node states + recent state changes
  POST /api/{device}/action/{name}   named action (anydesk/reboot/net/...)
  POST /api/{device}/shell           arbitrary command {"cmd": "..."}
  GET  /api/{device}/plug/status     that device's smart plug health
  POST /api/{device}/power/{op}      smart plug on|off|cycle
  POST /api/{device}/vps/offline     shut down device OS (plug untouched)
  POST /api/{device}/vps/online      wake device (plug cycle)
  GET  /api/{device}/night/status    night watch snapshot
  POST /api/{device}/lamp/{op}       lamp on|off|brightness {"pct": 50}
  GET  /api/{device}/net/status      rotation policy snapshot
  POST /api/{device}/net/rotate      rotate now {"to": "wifi|eth|sms"} optional
  POST /api/{device}/net/enable      {"on": true|false}
  POST /api/{device}/router/reboot   {"target": "dsl|eth|sms", "force": false}
  GET  /api/{device}/router/status   all boxes + public IP (asks the device)

  --- lsvc compatibility (loopback, X-Admin-Key) ---------------------------
  POST /api/night/keepalive          task ping; optional {"device": 1}
  POST /api/night/wake               task while device off; optional {"device": 1}
  POST /api/net/report               worker login-attempt/idle frame
                                     (optional "device", default 1)

  --- output API (loopback + X-Admin-Key: OUT_API_KEY) ---------------------
  GET  /out/v1/devices               device list + online/net summary
  GET  /out/v1/{device}/status       full status snapshot
  GET  /out/v1/{device}/net          rotation snapshot
  GET  /out/v1/events                recent events/alerts (ring buffer)
  POST /out/v1/{device}/control      {"action": name, "args": {...}}

The gate holds no business logic; it validates auth, moves frames, and exposes
the hub's request/response helper to the API and bot.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import (Depends, FastAPI, Header, HTTPException, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse

from hub import ControlLink, Hub
from services import DeviceServices

log = logging.getLogger("gate")

# router reboots park the route on a survivor box, reboot, wait for it to
# return and (maybe) switch back — several minutes end to end
ACTION_TIMEOUTS = {"shell": 120.0, "ps": 30.0, "router": 420.0,
                   "net": 90.0, "default": 45.0}


def build_app(hub: Hub, services: dict[int, DeviceServices], *,
              agent_token: str, admin_key: str,
              out_key: str = "") -> FastAPI:
    app = FastAPI(title="Local Server Gate")
    out_key = out_key or admin_key

    # ── auth dependencies ─────────────────────────────────────────────────
    def require_admin(x_admin_key: str = Header(default="")) -> None:
        if not admin_key or x_admin_key != admin_key:
            raise HTTPException(status_code=401, detail="unauthorized")

    def require_out(request: Request,
                    x_admin_key: str = Header(default="")) -> None:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="loopback only")
        if not out_key or x_admin_key != out_key:
            raise HTTPException(status_code=401, detail="unauthorized")

    def _check_agent_token(x_agent_token: str = Header(default="",
                                                        alias="X-Agent-Token")) -> None:
        if not agent_token or x_agent_token != agent_token:
            raise HTTPException(status_code=401, detail="unauthorized")

    def _svc(device: int) -> DeviceServices:
        svc = services.get(device)
        if svc is None:
            raise HTTPException(status_code=404, detail=f"unknown device {device}")
        return svc

    def _dev_or_404(device: int) -> int:
        if not hub.has_device(device):
            raise HTTPException(status_code=404, detail=f"unknown device {device}")
        return device

    # ── device status push (HTTP heartbeat) ───────────────────────────────
    @app.post("/status")
    async def device_status(request: Request,
                            _: None = Depends(_check_agent_token)):
        """Device posts full telemetry here every STATUS_INTERVAL (~60s).

        This drives the dead-man switch and the output API. Commands are NOT
        delivered on this endpoint — they go down the WebSocket /agent/ws.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be object")
        try:
            device = int(body.get("device", 1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bad device id")
        if not hub.has_device(device):
            raise HTTPException(status_code=404, detail=f"unknown device {device}")
        body.setdefault("host", body.get("host", "?"))
        hub.note_http_status(device, body)
        dev = hub.device(device)
        return {"ok": True, "device": device,
                "command_channel": dev.commands_available()}

    # ── control tunnel (commands down, results up) ────────────────────────
    @app.websocket("/agent/ws")
    async def agent_ws(ws: WebSocket):
        await ws.accept()
        # first frame must be a valid hello
        try:
            hello = json.loads(await ws.receive_text())
        except Exception:
            await ws.close(code=4000)
            return
        if hello.get("type") != "hello" or hello.get("token") != agent_token:
            await ws.close(code=4401)
            log.warning("control tunnel rejected (bad hello/token)")
            return
        try:
            device = int(hello.get("device", 1))
        except (TypeError, ValueError):
            device = 0
        if not hub.has_device(device):
            await ws.close(code=4404)
            log.warning("control tunnel rejected (unknown device %r)",
                        hello.get("device"))
            return

        async def _send(frame: dict) -> None:
            await ws.send_text(json.dumps(frame))

        link = ControlLink(_send)
        link.host = str(hello.get("host", "?"))
        await hub.attach_control(device, link)
        label = hub.device(device).label
        log.info("control tunnel up: device=%s host=%s", label, link.host)
        hub.add_event(device, f"{label}: command channel up")
        try:
            while True:
                frame = json.loads(await ws.receive_text())
                kind = frame.get("type")
                if kind == "telemetry":
                    hub.note_telemetry(device, frame)
                elif kind == "result":
                    hub.resolve_result(frame)
                elif kind == "heartbeat":
                    link.last_seen = time.time()
                elif kind == "event":
                    link.last_seen = time.time()
                    msg = str(frame.get("message", ""))
                    level = str(frame.get("level", "info"))
                    hub.add_event(device, msg, level)
                    if hub.on_event is not None:
                        try:
                            await hub.on_event(device, msg, level)
                        except Exception as exc:        # noqa: BLE001
                            log.warning("on_event failed: %s", exc)
                # ignore anything else
        except WebSocketDisconnect:
            pass
        except Exception as exc:                    # noqa: BLE001
            log.warning("control tunnel error: %s", exc)
        finally:
            await hub.detach_control(device, link)
            hub.add_event(device, f"{label}: command channel down", "warn")
            log.info("control tunnel down: device=%s host=%s", label, link.host)

    # ── admin API ─────────────────────────────────────────────────────────
    def _timeout_for(name: str) -> float:
        return ACTION_TIMEOUTS.get(name, ACTION_TIMEOUTS["default"])

    @app.get("/api/status")
    async def api_status(_: None = Depends(require_admin)):
        return {"ok": True, "devices": hub.all_snapshots()}

    @app.get("/api/cluster")
    async def api_cluster(_: None = Depends(require_admin)):
        cluster = getattr(hub, "cluster", None)
        if cluster is None:
            raise HTTPException(503, "cluster not running")
        cfw = getattr(hub, "cfwatch", None)
        return {"ok": True, **cluster.snapshot(),
                "dns": cfw.snapshot() if cfw is not None else None}

    @app.post("/api/{device}/drill")
    async def api_drill(device: int, request: Request,
                        _: None = Depends(require_admin)):
        health = getattr(hub, "health", None)
        if health is None:
            raise HTTPException(503, "health watch not running")
        body = await _json_body(request)
        _dev_or_404(device)
        return health.drill(device, str(body.get("what") or ""),
                            float(body.get("for_s") or 180))

    @app.get("/api/{device}/status")
    async def api_device_status(device: int, _: None = Depends(require_admin)):
        return hub.status_snapshot(_dev_or_404(device))

    @app.post("/api/{device}/action/{name}")
    async def api_action(device: int, name: str, request: Request,
                         _: None = Depends(require_admin)):
        args = await _json_body(request)
        return await _dispatch(hub, _dev_or_404(device), "action", name, args,
                               _timeout_for(name))

    @app.post("/api/{device}/shell")
    async def api_shell(device: int, request: Request,
                        _: None = Depends(require_admin)):
        body = await _json_body(request)
        cmd = body.get("cmd", "")
        if not cmd:
            raise HTTPException(status_code=400, detail="cmd required")
        return await _dispatch(hub, _dev_or_404(device), "shell", "shell",
                               {"cmd": cmd}, _timeout_for("shell"))

    @app.get("/api/{device}/plug/status")
    async def api_plug_status(device: int, _: None = Depends(require_admin)):
        return await _svc(device).plug.status()

    @app.post("/api/{device}/power/{op}")
    async def api_power(device: int, op: str,
                        _: None = Depends(require_admin)):
        plug = _svc(device).plug
        op = op.lower()
        if op == "on":
            return await plug.on()
        if op == "off":
            return await plug.off()
        if op == "cycle":
            return await plug.cycle()
        raise HTTPException(status_code=400, detail="op must be on|off|cycle")

    @app.post("/api/{device}/vps/offline")
    async def api_vps_offline(device: int, _: None = Depends(require_admin)):
        return await _svc(device).vps.offline()

    @app.post("/api/{device}/vps/online")
    async def api_vps_online(device: int, _: None = Depends(require_admin)):
        return await _svc(device).vps.online()

    @app.get("/api/{device}/night/status")
    async def api_night_status(device: int, _: None = Depends(require_admin)):
        return _svc(device).night.snapshot()

    @app.post("/api/{device}/lamp/{op}")
    async def api_lamp(device: int, op: str, request: Request,
                       _: None = Depends(require_admin)):
        plug = _svc(device).plug
        op = op.lower()
        if op == "on":
            return await plug.lamp(True)
        if op == "off":
            return await plug.lamp(False)
        if op == "brightness":
            body = await _json_body(request)
            pct = body.get("pct")
            if pct is None:
                raise HTTPException(status_code=400, detail="pct required (0-100)")
            return await plug.brightness(int(pct))
        raise HTTPException(status_code=400, detail="op must be on|off|brightness")

    # ── network rotation / router reboots ────────────────────────────────
    @app.get("/api/{device}/net/status")
    async def api_net_status(device: int, _: None = Depends(require_admin)):
        return _svc(device).netrotate.snapshot()

    @app.post("/api/{device}/net/rotate")
    async def api_net_rotate(device: int, request: Request,
                             _: None = Depends(require_admin)):
        body = await _json_body(request)
        return await _svc(device).netrotate.rotate_now(
            to=str(body.get("to") or ""), reason="manual (API)")

    @app.post("/api/{device}/net/enable")
    async def api_net_enable(device: int, request: Request,
                             _: None = Depends(require_admin)):
        body = await _json_body(request)
        on = body.get("on", True)
        if isinstance(on, str):
            on = on.lower() in ("1", "true", "yes", "on")
        return _svc(device).netrotate.set_enabled(bool(on))

    @app.post("/api/{device}/router/reboot")
    async def api_router_reboot(device: int, request: Request,
                                _: None = Depends(require_admin)):
        body = await _json_body(request)
        target = str(body.get("target") or "dsl")
        force = bool(body.get("force"))
        return await _svc(device).netrotate.reboot_box(
            target, why="manual (API)", force=force)

    @app.get("/api/{device}/router/status")
    async def api_router_status(device: int, _: None = Depends(require_admin)):
        return await _dispatch(hub, _dev_or_404(device), "action", "router",
                               {"op": "status"}, _timeout_for("default"))

    # ── lsvc compatibility (loopback callers, default device 1) ──────────
    async def _body_device(request: Request) -> tuple[dict, int]:
        body = await _json_body(request)
        try:
            device = int(body.get("device", 1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bad device id")
        return body, device

    @app.post("/api/night/keepalive")
    async def api_night_keepalive(request: Request,
                                  _: None = Depends(require_admin)):
        _body, device = await _body_device(request)
        return _svc(device).night.keepalive()

    @app.post("/api/night/wake")
    async def api_night_wake(request: Request,
                             _: None = Depends(require_admin)):
        _body, device = await _body_device(request)
        return await _svc(device).night.wake()

    @app.post("/api/net/report")
    async def api_net_report(request: Request,
                             _: None = Depends(require_admin)):
        body, device = await _body_device(request)
        return await _svc(device).netrotate.report(body)

    # ── output API (web view + control; loopback consumers) ──────────────
    @app.get("/out/v1/devices")
    async def out_devices(_: None = Depends(require_out)):
        out = []
        for snap in hub.all_snapshots():
            out.append({
                "device": snap["device"],
                "label": snap["label"],
                "online": snap["online"],
                "intentional_offline": snap["intentional_offline"],
                "command_channel": snap["command_channel"],
                "host": snap["host"],
                "net": snap["net"],
                "ip": snap["ip"],
                "internet": snap["internet"],
                "seconds_since_status": snap["seconds_since_status"],
            })
        return {"ok": True, "devices": out}

    @app.get("/out/v1/{device}/status")
    async def out_status(device: int, _: None = Depends(require_out)):
        return hub.status_snapshot(_dev_or_404(device))

    @app.get("/out/v1/{device}/net")
    async def out_net(device: int, _: None = Depends(require_out)):
        return _svc(device).netrotate.snapshot()

    @app.get("/out/v1/events")
    async def out_events(limit: int = 100, _: None = Depends(require_out)):
        limit = max(1, min(int(limit), 500))
        events = list(hub.events)[-limit:]
        return {"ok": True, "events": events}

    @app.post("/out/v1/{device}/control")
    async def out_control(device: int, request: Request,
                          _: None = Depends(require_out)):
        body = await _json_body(request)
        action = str(body.get("action") or "")
        if not action:
            raise HTTPException(status_code=400, detail="action required")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise HTTPException(status_code=400, detail="args must be object")
        return await _dispatch(hub, _dev_or_404(device), "action", action,
                               args, _timeout_for(action))

    return app


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def _dispatch(hub: Hub, device: int, kind: str, name: str, args: dict,
                    timeout: float) -> JSONResponse | dict:
    try:
        result = await hub.send_command(device, kind, name, args,
                                        timeout=timeout)
        return result
    except RuntimeError as exc:          # device offline
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except TimeoutError:
        return JSONResponse({"ok": False, "error": "timeout waiting for device"},
                            status_code=504)
