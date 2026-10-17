"""Tests for cfwatch.CfWatch against a fake Cloudflare API (httpx.MockTransport)."""
import asyncio
import json
import os
import tempfile
import unittest

import httpx

import cfwatch

IP = "203.0.113.10"
NAMES = ["gate.example.com", "tasks.example.com"]


class FakeCf:
    def __init__(self) -> None:
        self.records = {n: {"id": f"id-{n}", "name": n, "type": "A", "content": IP,
                            "proxied": False} for n in NAMES}
        self.down = False
        self.calls: list[tuple[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["Authorization"] == "Bearer tok123"
        if self.down:
            return httpx.Response(503, json={"success": False,
                                             "errors": [{"message": "down"}]})
        path = req.url.path
        if path.endswith("/zones"):
            return httpx.Response(200, json={"success": True, "result": [{"id": "z1"}]})
        if path.endswith("/dns_records") and req.method == "GET":
            rec = self.records.get(req.url.params["name"])
            return httpx.Response(200, json={"success": True,
                                             "result": [rec] if rec else []})
        if path.endswith("/dns_records") and req.method == "POST":
            body = json.loads(req.content)
            self.records[body["name"]] = {"id": "new", **body}
            return httpx.Response(200, json={"success": True, "result": body})
        if req.method == "PATCH":
            body = json.loads(req.content)
            for rec in self.records.values():
                if path.endswith(rec["id"]):
                    rec.update(body)
            return httpx.Response(200, json={"success": True, "result": {}})
        return httpx.Response(404, json={"success": False, "errors": []})


class CfWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.tok = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write("# limited token\ndns_cloudflare_api_token = tok123\n")
        self.cf = FakeCf()
        self.sent: list[str] = []
        self.answers = {n: [IP] for n in NAMES}

    def tearDown(self) -> None:
        os.unlink(self.tok)

    def make(self, proxied=False) -> cfwatch.CfWatch:
        async def notify(text):
            self.sent.append(text)

        async def resolve(name, server):
            return list(self.answers[name])

        client = httpx.AsyncClient(transport=httpx.MockTransport(self.cf.handler))
        return cfwatch.CfWatch(notify, self.tok, "example.com", NAMES, IP,
                               proxied=proxied, resolve=resolve, client=client)

    def run_once(self, w):
        return asyncio.run(w.check_once())

    def test_token_reader(self):
        self.assertEqual(cfwatch.read_token(self.tok), "tok123")

    def test_all_good(self):
        w = self.make()
        s = self.run_once(w)
        self.assertTrue(s["ok"])
        self.assertEqual(self.sent, [])
        self.assertIn("DNS watch: ok", w.status_line())

    def test_missing_record_is_created(self):
        del self.cf.records["tasks.example.com"]
        w = self.make()
        s = self.run_once(w)
        self.assertTrue(s["ok"])
        self.assertEqual(s["records"]["tasks.example.com"]["repaired"], "created")
        self.assertEqual(self.cf.records["tasks.example.com"]["content"], IP)
        self.assertIn("missing", self.sent[0])

    def test_changed_record_is_repaired(self):
        self.cf.records["gate.example.com"].update(content="1.2.3.4", proxied=True)
        w = self.make()
        s = self.run_once(w)
        self.assertIn("pointed to 1.2.3.4", s["records"]["gate.example.com"]["repaired"])
        self.assertEqual(self.cf.records["gate.example.com"]["content"], IP)
        self.assertFalse(self.cf.records["gate.example.com"]["proxied"])
        self.assertEqual(len(self.sent), 1)

    def test_api_down_alerts_once_then_recovers(self):
        self.cf.down = True
        w = self.make()
        s = self.run_once(w)
        self.assertFalse(s["ok"])
        self.run_once(w)
        self.assertEqual(sum("Cloudflare API" in t for t in self.sent), 1)
        self.assertIn("⚠️", w.status_line())
        self.cf.down = False
        s = self.run_once(w)
        self.assertTrue(s["ok"])
        self.assertIn("works again", self.sent[-1])

    def test_resolver_wrong_alerts_once(self):
        self.answers["gate.example.com"] = ["9.9.9.9"]
        w = self.make()
        self.run_once(w)
        self.run_once(w)
        bad = [t for t in self.sent if "answers gate.example.com" in t]
        self.assertEqual(len(bad), 2, "one per resolver, not repeated")
        self.answers["gate.example.com"] = [IP]
        self.run_once(w)
        self.assertIn("correctly again", self.sent[-1])

    def test_proxied_mode(self):
        for rec in self.cf.records.values():
            rec["proxied"] = True
        self.answers = {n: ["104.21.1.1", "172.67.1.1"] for n in NAMES}
        w = self.make(proxied=True)
        s = self.run_once(w)
        self.assertTrue(s["ok"], s)
        self.answers["tasks.example.com"] = [IP]      # origin IP leaking = bad
        s = self.run_once(w)
        self.assertFalse(s["ok"])
        self.cf.records["gate.example.com"]["proxied"] = False  # turned grey
        s = self.run_once(w)
        self.assertEqual(s["records"]["gate.example.com"]["repaired"], "DNS only")
        self.assertTrue(self.cf.records["gate.example.com"]["proxied"])


if __name__ == "__main__":
    unittest.main()
