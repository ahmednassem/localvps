"""Tests for gatelink.GateLink (domain first, IP fallback)."""
import asyncio
import ssl
import unittest

import gatelink

D = "wss://gate.example.com/agent/ws"
IP = "wss://203.0.113.10:443/agent/ws"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class GateLinkTest(unittest.TestCase):
    def make(self, **kw):
        self.clock = Clock()
        self.sent = []
        return gatelink.GateLink(D, IP, pin="AA:BB", clock=self.clock,
                                 notify=lambda t, lv: self.sent.append((lv, t)), **kw)

    def test_domain_first(self):
        g = self.make()
        self.assertEqual(g.pick(), "domain")
        self.assertEqual(g.url(g.pick()), D)
        self.assertEqual(g.pin, "aabb")

    def test_no_domain_means_ip_only(self):
        g = gatelink.GateLink("", IP)
        self.assertEqual(g.pick(), "ip")
        g.failed("ip", "down")
        self.assertEqual(g.pick(), "ip")

    def test_three_fails_move_to_ip_and_alert_after_15_min(self):
        g = self.make()
        for _ in range(2):
            g.failed(g.pick(), "timeout")
        self.assertEqual(g.pick(), "domain")
        g.failed("domain", "dns error")
        self.assertEqual(g.pick(), "ip")
        g.connected("ip")
        g.check_alert()
        self.assertEqual(self.sent, [], "no alert for a short stay on the IP")
        self.clock.t += 899
        g.check_alert()
        self.assertEqual(self.sent, [])
        self.clock.t += 2
        g.check_alert()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "warning")
        self.assertIn("dns error", self.sent[0][1])
        self.assertIn("15 min", self.sent[0][1])
        g.disconnected()
        g.connected("ip")
        g.check_alert()
        self.assertEqual(len(self.sent), 1, "one alert per stay on the IP")
        self.assertEqual(g.state()["link"], "ip")

    def test_short_stay_on_ip_is_silent_both_ways(self):
        g = self.make()
        for _ in range(3):
            g.failed("domain", "x")
        g.connected("ip")
        self.clock.t += 601
        self.assertEqual(g.pick(), "domain")
        g.connected("domain")
        self.assertEqual(self.sent, [])
        self.assertEqual(g.state()["on_ip_s"], 0)

    def test_retry_domain_after_10_min_on_next_connect(self):
        g = self.make()
        for _ in range(3):
            g.failed("domain", "x")
        g.connected("ip")
        g.failed("ip", "reset")          # IP drop => the VPS down => back to domain
        self.assertEqual(g.pick(), "domain")
        for _ in range(3):
            g.failed("domain", "x")
        self.assertEqual(g.pick(), "ip")
        g.connected("ip")
        self.clock.t += 601
        self.assertEqual(g.pick(), "domain")
        g.failed("domain", "still bad")  # a retry failing keeps the IP
        self.assertEqual(g.pick(), "ip")
        g.connected("ip")
        self.clock.t += 601
        g.check_alert()
        self.assertEqual(self.sent[-1][0], "warning")
        self.assertEqual(g.pick(), "domain")
        g.connected("domain")
        self.assertEqual(self.sent[-1][0], "info")
        self.assertIn("back on the domain", self.sent[-1][1])
        self.assertEqual(g.pick(), "domain")
        self.assertEqual(g.fails, 0)

    def test_probe_due_only_when_connected_on_ip(self):
        g = self.make()
        self.assertFalse(g.probe_due())
        for _ in range(3):
            g.failed("domain", "x")
        g.connected("ip")
        self.assertFalse(g.probe_due())
        self.clock.t += 601
        self.assertTrue(g.probe_due())

    def test_probe_success_makes_next_pick_domain(self):
        g = self.make()
        for _ in range(3):
            g.failed("domain", "x")
        g.connected("ip")
        self.clock.t += 601

        async def fake_open(*a, **kw):
            class W:
                def close(self):
                    pass
            return None, W()

        real = asyncio.open_connection
        asyncio.open_connection = fake_open
        try:
            ok = asyncio.run(g.probe())
        finally:
            asyncio.open_connection = real
        self.assertTrue(ok)
        self.assertEqual(g.pick(), "domain")

    def test_probe_failure_waits_again(self):
        g = self.make()
        for _ in range(3):
            g.failed("domain", "x")
        g.connected("ip")
        self.clock.t += 601

        async def bad_open(*a, **kw):
            raise OSError("refused")

        real = asyncio.open_connection
        asyncio.open_connection = bad_open
        try:
            ok = asyncio.run(g.probe())
        finally:
            asyncio.open_connection = real
        self.assertFalse(ok)
        self.assertFalse(g.probe_due())
        self.assertEqual(g.pick(), "ip")
        self.assertIn("refused", g.last_error)

    def test_ssl_contexts(self):
        g = self.make()
        dctx = g.ssl_context("domain")
        self.assertTrue(dctx.check_hostname)
        self.assertEqual(dctx.verify_mode, ssl.CERT_REQUIRED)
        ictx = g.ssl_context("ip")
        self.assertFalse(ictx.check_hostname)
        self.assertEqual(ictx.verify_mode, ssl.CERT_NONE)
        self.assertIsNone(gatelink.GateLink("", "ws://127.0.0.1:1/x").ssl_context("ip"))

    def test_pin_check(self):
        g = gatelink.GateLink(D, IP, pin=gatelink.cert_fingerprint(b"cert"))

        class Obj:
            def __init__(self, der):
                self.der = der

            def getpeercert(self, binary_form=False):
                return self.der

        g.check_pin("ip", Obj(b"cert"))
        g.check_pin("domain", Obj(b"other"))       # domain uses CA checks, not the pin
        with self.assertRaises(ssl.SSLError):
            g.check_pin("ip", Obj(b"other"))


if __name__ == "__main__":
    unittest.main()
