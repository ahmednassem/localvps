"""agent.py — connections from this device to the gate on the VPS.

Two outbound channels (the device dials out — no port forwarding). The gate is
reached on GATE_DOMAIN first and on the bare GATE_IP as fallback (gatelink.py):

  1. HTTP  POST https://<gate>/status  every ~60s
     → telemetry, dead-man heartbeat, dashboards

  2. WebSocket  wss://<gate>/agent/ws  (always connected)
     → Telegram/API commands pushed instantly; results returned on same socket

The domain has a real CA certificate. On the IP the gate's self-signed cert is
pinned by its SHA-256 fingerprint (GATE_TLS_FINGERPRINT) — see tlsutil.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

import actions
import gatelink
import status_push
import status_store
import telemetry
import tlsutil
from config import Config

log = logging.getLogger("agent")


class Agent:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._ctx = actions.ActionContext(cfg)
        self._ws = None
        self._connected = False
        self._down_since: float | None = None
        self._status_path = status_store.path_for(cfg)
        self._link = gatelink.GateLink(
            cfg.control_url_domain, cfg.control_url, cfg.gate_tls_fingerprint,
            notify=self._link_notify)

    @property
    def connected(self) -> bool:
        return self._connected

    def _link_notify(self, text: str, level: str) -> None:
        asyncio.get_running_loop().create_task(self.event(text, level=level))

    def _status_target(self) -> tuple[str, bool]:
        """(status URL, verify CA) for the link in use."""
        if self._cfg.status_url_domain and \
                (self._link.connected_mode or self._link.mode) == "domain":
            return self._cfg.status_url_domain, True
        return self._cfg.status_url, False

    def _connection_info(self) -> dict:
        if self._connected:
            mode = self._link.connected_mode or "ip"
            return {"state": "connected", "detail": self._link.url(mode), "link": mode}
        if self._down_since is not None:
            gap = int(time.time() - self._down_since)
            return {"state": "reconnecting", "detail": f"down {gap}s"}
        return {"state": "offline", "detail": "not connected"}

    async def run(self) -> None:
        if not self._cfg.agent_token:
            log.error("AGENT_TOKEN not configured")
            return
        if not self._cfg.status_url:
            log.warning("STATUS_URL not set — HTTP heartbeat disabled")
        if not self._cfg.control_url:
            log.warning("GATE_IP/CONTROL_URL not set — remote commands disabled")
        elif self._cfg.gate_tls and not self._cfg.control_url.startswith("wss://"):
            log.error("GATE_TLS is on but CONTROL_URL is not wss:// — got %s",
                      self._cfg.control_url)
            return

        status_task = asyncio.create_task(self._status_loop())
        try:
            await self._ws_loop()
        finally:
            status_task.cancel()

    async def _ws_loop(self) -> None:
        if not self._cfg.control_url:
            await asyncio.Future()  # block forever if no command channel
            return
        backoff = self._cfg.reconnect_min
        while True:
            mode = self._link.pick()
            url = self._link.url(mode)
            ssl_ctx = self._link.ssl_context(mode)
            up = False
            probe_task = None
            try:
                async with websockets.connect(
                    url,
                    ssl=ssl_ctx,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    ssl_obj = ws.transport.get_extra_info("ssl_object")
                    if mode == "ip" and ssl_obj is not None:
                        if not self._cfg.gate_tls_fingerprint:
                            fp = tlsutil.cert_fingerprint(
                                ssl_obj.getpeercert(binary_form=True))
                            log.warning(
                                "gate cert NOT pinned — set "
                                "GATE_TLS_FINGERPRINT=%s in .env", fp)
                        self._link.check_pin(mode, ssl_obj)
                    self._ws = ws
                    self._connected = True
                    self._down_since = None
                    status_store.clear_alert(self._status_path)
                    await ws.send(json.dumps({
                        "type": "hello",
                        "token": self._cfg.agent_token,
                        "device": self._cfg.device_id,
                        "host": self._cfg.host_name,
                    }))
                    up = True
                    self._link.connected(mode)
                    backoff = self._cfg.reconnect_min
                    log.info("command channel connected to %s (%s)", url, mode)
                    probe_task = asyncio.create_task(self._probe_domain(ws))
                    async for raw in ws:
                        await self._on_frame(raw)
            except Exception as exc:                 # noqa: BLE001
                log.warning("command channel down (%s): %s", mode, exc)
                if not up:
                    self._link.failed(mode, exc)
            finally:
                if probe_task is not None:
                    probe_task.cancel()
                if up:
                    self._link.disconnected()
                self._ws = None
                self._connected = False
                if self._down_since is None:
                    self._down_since = time.time()
                await self._maybe_alert_disconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._cfg.reconnect_max)

    async def _probe_domain(self, ws) -> None:
        """On the IP link: when the domain answers again, reconnect on it."""
        while True:
            await asyncio.sleep(30)
            self._link.check_alert()
            if self._link.probe_due() and await self._link.probe():
                log.info("gate domain reachable again - reconnecting on it")
                await ws.close()
                return

    async def _maybe_alert_disconnect(self) -> None:
        if self._down_since is None:
            return
        gap = time.time() - self._down_since
        if gap >= self._cfg.disconnect_alert_sec:
            status_store.set_alert(
                self._status_path,
                f"Command channel down ({int(gap)}s) — status HTTP may still work",
                level="warning",
            )

    async def _status_loop(self) -> None:
        while True:
            try:
                payload = await telemetry.collect(self._cfg)
                payload["host"] = self._cfg.host_name
                payload["device"] = self._cfg.device_id
                payload["connection"] = self._connection_info()
                payload["link"] = self._link.state()
                status_store.merge_telemetry(
                    self._status_path, payload,
                    connection=self._connection_info(),
                    host=self._cfg.host_name,
                )
                await self._post_status(payload)
            except Exception as exc:                 # noqa: BLE001
                log.debug("status loop error: %s", exc)
            await asyncio.sleep(self._cfg.status_interval)

    async def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except Exception:                            # noqa: BLE001
            return
        if frame.get("type") != "cmd":
            return
        asyncio.create_task(self._handle_cmd(frame))

    async def _handle_cmd(self, frame: dict) -> None:
        cmd_id = frame.get("id")
        kind = frame.get("kind", "action")
        name = frame.get("name", "")
        args = frame.get("args", {}) or {}
        result = await actions.dispatch(kind, name, args, self._ctx)
        result.update({"type": "result", "id": cmd_id})
        await self._send(result)

    async def _send(self, frame: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(frame))

    async def notify(self, text: str) -> None:
        await self.event(text, level="critical")

    async def event(self, text: str, level: str = "info") -> None:
        status_store.set_alert(self._status_path, text, level=level)
        try:
            await self._send({"type": "event", "level": level, "message": text})
        except Exception:                            # noqa: BLE001
            pass

    async def push_status_now(self, extra: dict | None = None) -> None:
        """Immediate telemetry POST (e.g. after wake) so the cloud sees the device."""
        payload = await telemetry.collect(self._cfg)
        payload["host"] = self._cfg.host_name
        payload["device"] = self._cfg.device_id
        payload["connection"] = self._connection_info()
        payload["link"] = self._link.state()
        if extra:
            payload.update(extra)
        status_store.merge_telemetry(
            self._status_path, payload,
            connection=self._connection_info(),
            host=self._cfg.host_name,
        )
        await self._post_status(payload)

    async def _post_status(self, payload: dict) -> None:
        """Beat on the link in use; a failed beat on the domain goes to the IP."""
        url, verify = self._status_target()
        if not url:
            return
        res = await status_push.post_async(
            url, self._cfg.agent_token, payload, verify=verify)
        if verify and not res.get("ok", True) and self._cfg.status_url:
            log.warning("status beat on the domain failed (%s) - sent to the IP",
                        res.get("error"))
            await status_push.post_async(
                self._cfg.status_url, self._cfg.agent_token, payload)

    async def notify_wake(self, gap_s: float) -> None:
        import netctl
        src, ip = await netctl.active_source(self._cfg)
        label = {"wifi": "Wi‑Fi", "eth": "Ethernet", "sms": "SMS"}.get(
            (src or "").split(":")[0], src or "unknown")
        msg = (
            f"💻 {self._cfg.host_name} woke up — laptop is online "
            f"({label}, {ip or 'no ip'}, slept ~{int(gap_s)}s)"
        )
        log.info("wake notify: %s", msg)
        await self.event(msg, level="info")
        await self.push_status_now({"wake": True, "wake_gap_s": int(gap_s)})
