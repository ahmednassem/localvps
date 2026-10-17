"""lsvc_feed.py — push device state to the local-services gate (lsvc, :8102).

The local services on the VPS need to know which device is working (device 1
or device 2). We push over loopback whenever a device's
state changes (hub.on_device_change) and refresh periodically so lsvc never
works from stale data after one of the two processes restarts.

lsvc side: POST {LSVC_URL}/localserver/devices  (X-Admin-Key: LSVC_KEY)
body: {"devices": [{"device", "label", "online", "command_channel",
                    "host", "net", "ip", "ts"}, ...]}
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from hub import Hub

log = logging.getLogger("lsvc_feed")


class LsvcFeed:
    def __init__(self, hub: Hub, url: str, key: str, *,
                 refresh_s: float = 60.0) -> None:
        self._hub = hub
        self._url = url.rstrip("/")
        self._key = key
        self._refresh = refresh_s
        self._push_wanted = asyncio.Event()
        self._last_error_log = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    async def on_device_change(self, device_id: int) -> None:
        """Hub callback — a device came online/offline or its link moved."""
        self._push_wanted.set()

    def _payload(self) -> dict:
        devices = []
        for snap in self._hub.all_snapshots():
            devices.append({
                "device": snap["device"],
                "label": snap["label"],
                "online": snap["online"],
                "intentional_offline": snap["intentional_offline"],
                "command_channel": snap["command_channel"] == "connected",
                "host": snap["host"],
                "net": snap["net"],
                "ip": snap["ip"],
                "ts": time.time(),
            })
        return {"devices": devices}

    async def _push(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._url}/localserver/devices",
                    json=self._payload(),
                    headers={"X-Admin-Key": self._key})
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:                        # noqa: BLE001
            # lsvc may be down/restarting — log at most once a minute
            now = time.time()
            if now - self._last_error_log > 60:
                self._last_error_log = now
                log.warning("device feed push failed: %s", exc)

    async def run(self) -> None:
        if not self.configured:
            log.info("lsvc feed disabled (LSVC_URL/LSVC_KEY not set)")
            return
        log.info("lsvc feed started (%s, refresh=%ss)", self._url, self._refresh)
        while True:
            try:
                await asyncio.wait_for(self._push_wanted.wait(),
                                       timeout=self._refresh)
            except asyncio.TimeoutError:
                pass                                    # periodic refresh
            self._push_wanted.clear()
            await self._push()
