"""Offline checks for plugs.py against a fake OpenBeken plug (local HTTP).
Run: python test_plugs_obk.py"""
import asyncio
import base64
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Plug:
    power = False
    lamp = True
    watts = 0.0
    password = "pw2"
    calls = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        want = "Basic " + base64.b64encode(f"admin:{Plug.password}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.end_headers()
            return
        q = urllib.parse.urlparse(self.path)
        cmnd = urllib.parse.parse_qs(q.query).get("cmnd", [""])[0]
        Plug.calls.append(cmnd)
        if q.path != "/cm":
            body = "<html>not here</html>"
        elif cmnd in ("Power1 On", "Power1 Off", "Power1", "Power2"):
            if cmnd != "Power1" and cmnd != "Power2":
                Plug.power = cmnd.endswith("On")
            # OpenBeken answers every PowerN query with all channels
            body = json.dumps({"POWER1": "ON" if Plug.power else "OFF",
                               "POWER2": "ON" if Plug.lamp else "OFF"})
        elif cmnd == "STATUS 8":
            body = json.dumps({"StatusSNS": {"ENERGY": {
                "Power": Plug.watts, "Voltage": 229.04, "ConsumptionTotal": 1.25012}}})
        else:
            body = "garbage"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
host = f"127.0.0.1:{srv.server_address[1]}"

os.environ.update({"PLUG2_HOST": host, "PLUG2_PASS": "pw2", "PLUG1_HOST": ""})
import plugs  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        fails.append(name)


async def main():
    check("configured by host", (plugs.configured(1), plugs.configured(2)), (False, True))

    r = await plugs.run("on", 2)
    check("on", (r["ok"], Plug.power), (True, True))
    check("lamp untouched", Plug.lamp, True)
    Plug.watts = 18.4
    s = await plugs.run("status", 2)
    check("status", (s["ok"], s["socket"], s["lamp"], s["watts"], s["volts"], s["kwh"]),
          (True, True, True, 18.4, 229.0, 1.25))
    check("laptop running", s["laptop_running"], True)

    r = await plugs.run("cycle", 2, gap=0.05)
    check("cycle", (r["ok"], Plug.power), (True, True))
    check("cycle order", Plug.calls[-2:], ["Power1 Off", "Power1 On"])

    r = await plugs.run("off", 2)
    check("off", (r["ok"], Plug.power), (True, False))
    s = await plugs.run("status", 2)
    check("off: not running", (s["socket"], s["laptop_running"]), (False, False))

    os.environ["PLUG2_PASS"] = "wrong"
    r = await plugs.run("on", 2)
    check("wrong password -> clean error", (r["ok"], Plug.power), (False, False))
    os.environ["PLUG2_PASS"] = "pw2"

    os.environ["PLUG2_HOST"] = "127.0.0.1:1"
    r = await plugs.run("status", 2)
    check("plug gone -> clean error", r["ok"], False)
    os.environ["PLUG2_HOST"] = host

    check("plug 1 unconfigured", (await plugs.run("on", 1))["ok"], False)

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    srv.shutdown()
    sys.exit(1 if fails else 0)


asyncio.run(main())
