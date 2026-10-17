"""Offline test of the node loopback API (local_api.py)."""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request

import local_api

fails = []
KEY = "k" * 40
PORT = 18711


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


class Link:
    def __init__(self):
        self.handlers = {}
        self.sent = []

    def on(self, kind, fn):
        self.handlers[kind] = fn

    connected = True

    def status(self):
        return {"connected": True, "rtt_ms": 3.0}

    async def request(self, kind, body, timeout):
        self.sent.append((kind, body, timeout))
        return {"ok": True, "echo": body}


def call(method, path, body=None, key=KEY):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Cluster-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def acall(*a, **k):
    return await asyncio.to_thread(call, *a, **k)


async def main():
    link = Link()
    plugs = []

    async def plug(op, n):
        plugs.append((op, n))
        return {"ok": True, "state": "on"}

    api = local_api.LocalApi(1, link, self_state=lambda: {"vps_link": True},
                             plug=plug, key=KEY, port=PORT)
    srv = asyncio.create_task(api.run())
    await asyncio.sleep(0.3)

    check("bad key", (await acall("GET", "/cluster/self", key="nope"))[0], 401)
    s, b = await acall("GET", "/cluster/self")
    check("self", (s, b["device"], b["vps_link"], b["worker_listening"]), (200, 1, True, False))
    s, b = await acall("GET", "/cluster/peer")
    check("peer", (s, b["connected"]), (200, True))
    s, b = await acall("POST", "/cluster/peer/send", {"kind": "echo", "body": {"x": 1}})
    check("peer send", (s, b["echo"]), (200, {"x": 1}))
    s, b = await acall("POST", "/cluster/power/2/off")
    check("power off refused", s, 400)
    s, b = await acall("POST", "/cluster/power/2/on")
    check("power on", (s, plugs), (200, [("on", 2)]))

    offer = link.handlers["handoff_offer"]
    r = await offer({"task_id": "t1"})
    check("offer with no worker polling", r["ok"], False)

    async def worker():
        s, item = await acall("GET", "/cluster/inbox?wait=5")
        check("inbox item", (s, item["kind"], item["body"]), (200, "handoff_offer", {"task_id": "t2"}))
        s, b = await acall("POST", f"/cluster/inbox/{item['id']}", {"ok": True, "took": "t2"})
        check("reply accepted", b["ok"], True)

    s, _ = await acall("GET", "/cluster/inbox?wait=0.2")
    check("empty inbox -> 204", s, 204)
    w = asyncio.create_task(worker())
    await asyncio.sleep(0.3)
    r = await offer({"task_id": "t2"})
    check("peer gets the worker's reply", r, {"ok": True, "took": "t2"})
    await w
    s, b = await acall("POST", "/cluster/inbox/999", {"ok": True})
    check("stale reply", b["ok"], False)
    check("404", (await acall("GET", "/nope"))[0], 404)

    srv.cancel()
    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
