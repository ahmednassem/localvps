"""Node loopback API on 127.0.0.1:8711 (phase 4).

Local programs (the task worker) talk to the cluster through this agent:

  GET  /cluster/self                 this node: the VPS link, peer, lead fallback
  GET  /cluster/peer                 the peer link status
  POST /cluster/peer/send            {"kind", "body", "timeout"} -> the peer's reply
  POST /cluster/power/{node}/{op}    plug status | on (off stays with the VPS / the lead rules)
  GET  /cluster/inbox?wait=25        next message the peer sent to the worker, or 204
  POST /cluster/inbox/{id}           the worker's reply to that message

Every call needs the header X-Cluster-Key with the key in `api_key.txt`
(created here on first start; the worker on the same device reads it).
Peer messages of the kinds in INBOX_KINDS wait in the inbox for the worker and
are answered with its reply, or {"ok": false} when no worker picks them up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from itertools import count
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("local_api")

HOST = "127.0.0.1"
PORT = 8711
KEY_FILE = Path(__file__).resolve().parent / "api_key.txt"
INBOX_KINDS = ("handoff_offer",)
WORKER_REPLY_S = 8.0
MAX_BODY = 1 << 20

_REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
            404: "Not Found", 405: "Method Not Allowed", 413: "Too Large"}


def load_key(path: Path = KEY_FILE) -> str:
    try:
        key = path.read_text(encoding="ascii").strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(24)
    path.write_text(key, encoding="ascii")
    return key


class LocalApi:
    def __init__(self, device_id: int, link, *, self_state: Callable[[], dict],
                 plug: Callable[[str, int], Awaitable[dict]],
                 key: str | None = None, host: str = HOST, port: int = PORT) -> None:
        self.device_id = device_id
        self._link = link
        self._self_state = self_state
        self._plug = plug
        self._key = key
        self._host, self._port = host, port
        self._ids = count(1)
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._waiting: dict[int, asyncio.Future] = {}
        self._last_poll = 0.0
        if link is not None:
            for kind in INBOX_KINDS:
                link.on(kind, self._make_inbox_handler(kind))

    # ── peer messages for the worker ──────────────────────────────────────
    def _make_inbox_handler(self, kind: str):
        async def handler(body: dict) -> dict:
            return await self.to_worker(kind, body)
        return handler

    def worker_listening(self) -> bool:
        return time.time() - self._last_poll < 40

    async def to_worker(self, kind: str, body: dict) -> dict:
        if not self.worker_listening():
            return {"ok": False, "error": "worker not listening"}
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[mid] = fut
        try:
            self._inbox.put_nowait({"id": mid, "kind": kind, "body": body})
            return await asyncio.wait_for(fut, WORKER_REPLY_S)
        except asyncio.QueueFull:
            return {"ok": False, "error": "worker inbox full"}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "worker did not answer"}
        finally:
            self._waiting.pop(mid, None)

    # ── http ──────────────────────────────────────────────────────────────
    async def run(self) -> None:
        if self._key is None:
            self._key = await asyncio.to_thread(load_key)
        server = await asyncio.start_server(self._client, self._host, self._port)
        log.info("local api on %s:%d", self._host, self._port)
        async with server:
            await server.serve_forever()

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            status, out = await asyncio.wait_for(self._one(reader), 60)
        except asyncio.TimeoutError:
            status, out = 400, {"ok": False, "error": "timeout"}
        except Exception as exc:                          # noqa: BLE001
            log.warning("local api error: %s", exc)
            status, out = 400, {"ok": False, "error": str(exc)}
        body = b"" if status == 204 else json.dumps(out).encode()
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode()
        try:
            writer.write(head + body)
            await writer.drain()
        finally:
            writer.close()

    async def _one(self, reader: asyncio.StreamReader) -> tuple[int, dict]:
        line = (await reader.readline()).decode("latin-1").strip()
        parts = line.split()
        if len(parts) < 2:
            return 400, {"ok": False, "error": "bad request line"}
        method, target = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        while True:
            h = (await reader.readline()).decode("latin-1")
            if h in ("\r\n", "\n", ""):
                break
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
        size = int(headers.get("content-length") or 0)
        if size > MAX_BODY:
            return 413, {"ok": False, "error": "body too large"}
        raw = await reader.readexactly(size) if size else b""
        if not secrets.compare_digest(headers.get("x-cluster-key", ""), self._key or ""):
            return 401, {"ok": False, "error": "bad key"}
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return 400, {"ok": False, "error": "body is not json"}
        url = urlsplit(target)
        return await self.handle(method, url.path.rstrip("/"), parse_qs(url.query),
                                 data if isinstance(data, dict) else {})

    async def handle(self, method: str, path: str, query: dict,
                     data: dict) -> tuple[int, dict]:
        seg = [s for s in path.split("/") if s]
        if seg[:1] != ["cluster"]:
            return 404, {"ok": False, "error": "not found"}
        seg = seg[1:]
        if seg == ["self"] and method == "GET":
            return 200, {"ok": True, "device": self.device_id,
                         "worker_listening": self.worker_listening(),
                         **self._self_state()}
        if seg == ["peer"] and method == "GET":
            st = self._link.status() if self._link else {"enabled": False}
            return 200, {"ok": True, **st}
        if seg == ["peer", "send"] and method == "POST":
            if self._link is None:
                return 200, {"ok": False, "error": "peer link off"}
            kind = str(data.get("kind") or "")
            if not kind:
                return 400, {"ok": False, "error": "kind required"}
            timeout = min(max(float(data.get("timeout") or 10), 1.0), 30.0)
            return 200, await self._link.request(kind, data.get("body") or {}, timeout)
        if len(seg) == 3 and seg[0] == "power" and method == "POST":
            try:
                node = int(seg[1])
            except ValueError:
                return 400, {"ok": False, "error": "node must be 1 or 2"}
            if seg[2] not in ("status", "on"):
                return 400, {"ok": False, "error": "op must be status|on"}
            return 200, await self._plug(seg[2], node)
        if seg == ["inbox"] and method == "GET":
            self._last_poll = time.time()
            wait = min(max(float((query.get("wait") or ["25"])[0]), 0.0), 30.0)
            try:
                item = await asyncio.wait_for(self._inbox.get(), wait)
            except asyncio.TimeoutError:
                self._last_poll = time.time()
                return 204, {}
            self._last_poll = time.time()
            return 200, {"ok": True, **item}
        if len(seg) == 2 and seg[0] == "inbox" and method == "POST":
            try:
                fut = self._waiting.get(int(seg[1]))
            except ValueError:
                fut = None
            if fut is None or fut.done():
                return 200, {"ok": False, "error": "no such message (expired)"}
            fut.set_result(data)
            return 200, {"ok": True}
        return 404, {"ok": False, "error": "not found"}


API: "LocalApi | None" = None
