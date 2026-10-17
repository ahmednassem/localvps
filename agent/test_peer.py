"""Offline test of peer.py: two links on localhost (no LAN, no beacons)."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import peer

PORT = 18710


def _pair(tmp: Path, *, token_b: str = "tok", fp: str | None = None):
    peer.CERT = tmp / "peer_cert.pem"
    peer.KEY = tmp / "peer_key.pem"
    real_fp = peer.ensure_cert(2)
    a = peer.PeerLink(1, token="tok", peer_fp=real_fp if fp is None else fp,
                      port=PORT, static_host="127.0.0.1",
                      state=lambda: {"vps_link": True})
    b = peer.PeerLink(2, token=token_b, port=PORT,
                      state=lambda: {"vps_link": False})
    for link in (a, b):
        link._beacon_send = link._beacon_listen = _idle
    return a, b


async def _idle():
    await asyncio.Event().wait()


async def _run(a, b, secs: float):
    tasks = [asyncio.create_task(b.run()), asyncio.create_task(a.run())]
    await asyncio.sleep(secs)
    return tasks


def _stop(tasks):
    for t in tasks:
        t.cancel()


async def main() -> int:
    fails = 0

    def check(name, ok):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name)
        fails += 0 if ok else 1

    peer.BEAT_S = 0.5
    tmp = Path(tempfile.mkdtemp())

    a, b = _pair(tmp)
    tasks = await _run(a, b, 2.0)
    check("link up both sides", a.connected and b.connected)
    check("rtt measured", a.status()["rtt_ms"] is not None)
    check("peer state carried", a.status()["peer_state"] == {"vps_link": False}
          and b.status()["peer_state"] == {"vps_link": True})
    res = await a.request("echo", {"x": 1})
    check("request/reply", res.get("ok") and res.get("echo") == {"x": 1}
          and res.get("device") == 2)
    res = await b.request("nope")
    check("unknown kind refused", res.get("ok") is False)

    async def slow(body):
        await asyncio.sleep(5)
        return {}
    b.on("slow", slow)
    res = await a.request("slow", timeout=0.5)
    check("request timeout", res.get("error") == "peer timeout")
    _stop(tasks)
    await asyncio.sleep(0.3)

    a, b = _pair(tmp, fp="00" * 32)
    tasks = await _run(a, b, 1.5)
    check("wrong fingerprint refused", not a.connected and not b.connected
          and "fingerprint" in a.status()["error"])
    _stop(tasks)
    await asyncio.sleep(0.3)

    a, b = _pair(tmp, token_b="other")
    tasks = await _run(a, b, 1.5)
    check("wrong token refused", not a.connected and not b.connected)
    _stop(tasks)
    await asyncio.sleep(0.3)

    a, b = _pair(tmp, fp="")
    tasks = await _run(a, b, 1.0)
    check("no pin, no dial", not a.connected
          and a.status()["error"] == "PEER_FINGERPRINT not set")
    _stop(tasks)
    await asyncio.sleep(0.3)

    peer.DEAD_S = 1.5
    a, b = _pair(tmp)
    tasks = await _run(a, b, 1.5)
    up = a.connected
    a._beat = b._beat = lambda conn: _idle()
    for link in (a, b):
        if link._conn:
            link._conn.last_rx = 0
    b._conn.writer.transport.pause_reading()
    a._conn.writer.transport.pause_reading()
    await asyncio.sleep(2.5)
    check("dead link detected", up and not b.connected)
    _stop(tasks)

    print("OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
