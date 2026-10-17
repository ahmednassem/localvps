"""status_push.py — POST telemetry to the gate over TLS.

On the gate domain the certificate is checked normally (verify=True). On the
bare IP the gate serves a self-signed certificate, so the POST uses an
encrypted-but-unverified context from tlsutil. Commands never travel on this
channel — the control WebSocket is the pinned one. Plain http:// is allowed
only when GATE_TLS=false (testing).
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request

import tlsutil

log = logging.getLogger("status_push")


def post(url: str, token: str, payload: dict, timeout: float = 20.0,
         verify: bool = False) -> dict:
    """verify=True: the gate domain (real CA cert, checked normally)."""
    ctx = None
    if url.startswith("https://"):
        ctx = ssl.create_default_context() if verify else tlsutil.client_context()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Agent-Token", token)
    # Cloudflare's browser check bans urllib's default agent (error 1010).
    req.add_header("User-Agent", "LocalServerAgent/1")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as exc:
        log.warning("status POST failed HTTP %s: %s", exc.code, exc.read()[:200])
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:                            # noqa: BLE001
        log.warning("status POST failed: %s", exc)
        return {"ok": False, "error": str(exc)}


async def post_async(url: str, token: str, payload: dict, timeout: float = 20.0,
                     verify: bool = False) -> dict:
    import asyncio
    return await asyncio.to_thread(post, url, token, payload, timeout, verify)
