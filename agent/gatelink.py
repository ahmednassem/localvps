"""gatelink.py — which address reaches the gate: domain first, bare IP fallback.

The domain (gate.example.com / tasks.example.com) has a real Let's Encrypt
certificate and is checked the normal way. The bare IP serves a self-signed
certificate that is pinned by its SHA-256 fingerprint.

  - FAILS_TO_IP connects in a row fail on the domain -> use the IP.
  - On the IP, the domain is tried again every RETRY_DOMAIN_S: probe() while
    connected (a verified TLS handshake), or on the next connect attempt.
  - If the IP fails too, the VPS itself is down: go back to trying the domain.
  - An alert goes out once a device has been on the IP for ALERT_AFTER_S
    (check_alert(), called every 30 s), and again when it is back.

The same file is used by the local server agent and the task worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger("gatelink")

FAILS_TO_IP = 3
RETRY_DOMAIN_S = 600.0
ALERT_AFTER_S = 900.0


def cert_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


class GateLink:
    def __init__(self, domain_url: str, ip_url: str, pin: str = "",
                 notify: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 fails_to_ip: int = FAILS_TO_IP,
                 retry_s: float = RETRY_DOMAIN_S,
                 alert_after_s: float = ALERT_AFTER_S) -> None:
        self.domain_url = (domain_url or "").strip()
        self.ip_url = (ip_url or "").strip()
        self.pin = (pin or "").replace(":", "").strip().lower()
        self.notify = notify
        self.clock = clock
        self.fails_to_ip = fails_to_ip
        self.retry_s = retry_s
        self.alert_after_s = alert_after_s
        self.mode = "domain" if self.domain_url else "ip"
        self.fails = 0
        self.last_error = ""
        self.domain_tried = 0.0
        self.ip_since: float | None = None
        self.on_ip_alerted = False
        self.switches = 0
        self.connected_mode: str | None = None

    # ── choosing ─────────────────────────────────────────────────────────
    def url(self, mode: str) -> str:
        return self.domain_url if mode == "domain" else self.ip_url

    def pick(self) -> str:
        """Mode to use for the next connect attempt."""
        if self.mode == "ip" and self.domain_url and \
                self.clock() - self.domain_tried >= self.retry_s:
            self.domain_tried = self.clock()
            return "domain"
        return self.mode

    def ssl_context(self, mode: str) -> ssl.SSLContext | None:
        if not self.url(mode).startswith(("wss://", "https://")):
            return None
        ctx = ssl.create_default_context()
        if mode == "ip":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def check_pin(self, mode: str, ssl_object) -> None:
        """Raise if the IP link's certificate is not the pinned one."""
        if mode != "ip" or not self.pin or ssl_object is None:
            return
        der = ssl_object.getpeercert(binary_form=True)
        got = cert_fingerprint(der) if der else ""
        if got != self.pin:
            raise ssl.SSLError(
                f"gate certificate fingerprint mismatch (got {got[:16]})")

    # ── outcomes ─────────────────────────────────────────────────────────
    def connected(self, mode: str) -> None:
        now = self.clock()
        self.connected_mode = mode
        if mode == "domain":
            self.fails = 0
            if self.mode == "ip":
                self.switches += 1
            self.mode = "domain"
            if self.on_ip_alerted:
                mins = int((now - (self.ip_since or now)) / 60)
                self._notify(f"gate link back on the domain (was on the IP {mins} min)",
                             "info")
            self.on_ip_alerted = False
            self.ip_since = None
        elif self.domain_url and self.ip_since is None:
            self.ip_since = now

    def check_alert(self) -> None:
        """Alert once when connected on the IP for longer than alert_after_s."""
        if (self.connected_mode == "ip" and self.domain_url and not self.on_ip_alerted
                and self.ip_since is not None
                and self.clock() - self.ip_since >= self.alert_after_s):
            self.on_ip_alerted = True
            mins = int((self.clock() - self.ip_since) / 60)
            self._notify(f"gate link on the bare IP for {mins} min - domain failing: "
                         f"{self.last_error or 'unknown'}", "warning")

    def disconnected(self) -> None:
        self.connected_mode = None

    def failed(self, mode: str, err: object) -> None:
        self.last_error = str(err)[:200]
        self.connected_mode = None
        if mode == "ip":
            if self.domain_url and self.mode == "ip":
                log.info("IP link failed too (%s) - back to trying the domain",
                         self.last_error)
                self.mode = "domain"
                self.fails = 0
            return
        if self.mode == "ip":
            return
        self.fails += 1
        if self.fails >= self.fails_to_ip and self.ip_url:
            now = self.clock()
            log.warning("domain failed %d times (%s) - using the IP",
                        self.fails, self.last_error)
            self.mode = "ip"
            self.switches += 1
            self.domain_tried = now

    # ── probe while on the IP ────────────────────────────────────────────
    def probe_due(self) -> bool:
        return (self.connected_mode == "ip" and bool(self.domain_url)
                and self.clock() - self.domain_tried >= self.retry_s)

    async def probe(self, timeout: float = 10.0) -> bool:
        """Verified TLS handshake to the domain. True = switch back now."""
        self.domain_tried = self.clock()
        parts = urlsplit(self.domain_url)
        host = parts.hostname or ""
        port = parts.port or 443
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(
                host, port, ssl=ssl.create_default_context(),
                server_hostname=host), timeout)
            writer.close()
        except Exception as exc:                     # noqa: BLE001
            self.last_error = str(exc)[:200]
            log.info("domain probe failed: %s", self.last_error)
            return False
        self.domain_tried = 0.0
        return True

    # ── reporting ────────────────────────────────────────────────────────
    def state(self) -> dict:
        mode = self.connected_mode or self.mode
        url = self.url(mode)
        return {
            "link": mode,
            "host": urlsplit(url).hostname if url else "",
            "connected": self.connected_mode is not None,
            "fails": self.fails,
            "switches": self.switches,
            "on_ip_s": int(self.clock() - self.ip_since) if self.ip_since else 0,
            "last_error": self.last_error,
        }

    def _notify(self, text: str, level: str) -> None:
        log.info(text)
        if self.notify is not None:
            try:
                self.notify(text, level)
            except Exception:                        # noqa: BLE001
                log.debug("notify failed", exc_info=True)
