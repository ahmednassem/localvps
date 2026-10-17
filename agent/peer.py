"""peer.py — direct link between the two home devices (cluster plan phase 2).

One TLS connection over the home LAN on PEER_PORT. The lower device id dials,
the other listens. Each device has its own self-signed certificate
(peer_cert.pem / peer_key.pem, made on first start); the dialer pins the
listener's certificate fingerprint and both sides prove CLUSTER_TOKEN.

Frames are one JSON object per line:
  hello   {device, token}            first frame from each side
  beat    {t, state}                 every BEAT_S; answered with beat_ack {t}
  msg     {id, kind, body}           request; answered with reply {id, ok, body}

The link is dead after DEAD_S without any frame; the dialer then retries.

Finding the other device: a UDP beacon on BEACON_PORT (broadcast every
BEACON_S), a hint pushed by the VPS (agent action peer_hint), then PEER_HOST.

.env:
  CLUSTER_TOKEN       shared secret, same on both devices (link off if empty)
  PEER_FINGERPRINT    sha256 of the other device's peer_cert.pem (the dialer
                      refuses the link without it)
  PEER_PORT           default 8710
  PEER_BEACON_PORT    default 8712
  PEER_HOST           optional fixed LAN address of the other device
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import itertools
import json
import logging
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("peer")

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
CERT = _HERE / "peer_cert.pem"
KEY = _HERE / "peer_key.pem"

BEAT_S = 5.0
DEAD_S = 20.0
BEACON_S = 10.0
BEACON_FRESH_S = 60.0
DIAL_GAP_S = 5.0
MAX_LINE = 1 << 20
ROUTER = "192.168.0.1"

Handler = Callable[[dict], Awaitable[dict]]

LINK: "PeerLink | None" = None


def snapshot() -> dict:
    return LINK.status() if LINK else {"enabled": False}


# ── certificate ───────────────────────────────────────────────────────────

def ensure_cert(device_id: int) -> str:
    """Make this device's certificate once; return its sha256 fingerprint."""
    if not (CERT.exists() and KEY.exists()):
        if str(_LIB) not in sys.path:
            sys.path.append(str(_LIB))
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                             f"localvps-node-{device_id}")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        KEY.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        log.info("peer certificate created")
    return fingerprint_pem(CERT.read_text())


def fingerprint_pem(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


def lan_ip() -> str | None:
    """This device's address on the home LAN (the route to the router)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ROUTER, 9))
        ip = s.getsockname()[0]
        return ip if ip.startswith("192.168.1.") else None
    except OSError:
        return None
    finally:
        s.close()


# ── link ──────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, role: str, host: str) -> None:
        self.reader = reader
        self.writer = writer
        self.role = role
        self.host = host
        self.peer_device: int | None = None
        self.last_rx = time.time()
        self._wlock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        data = (json.dumps(frame, separators=(",", ":")) + "\n").encode()
        async with self._wlock:
            self.writer.write(data)
            await self.writer.drain()

    async def recv(self) -> dict:
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("closed")
        if len(line) > MAX_LINE:
            raise ConnectionError("frame too large")
        self.last_rx = time.time()
        return json.loads(line)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:                            # noqa: BLE001
            pass


class PeerLink:
    def __init__(self, device_id: int, *, token: str, peer_fp: str = "",
                 port: int = 8710, beacon_port: int = 8712,
                 static_host: str = "",
                 state: Callable[[], dict] | None = None) -> None:
        self.device_id = device_id
        self.peer_id = 2 if device_id == 1 else 1
        self.dialer = device_id < self.peer_id
        self._token = token
        self._peer_fp = _norm_fp(peer_fp)
        self.port = port
        self.beacon_port = beacon_port
        self._static = static_host
        self._state = state or (lambda: {})
        self.fp = ""
        self._conn: _Conn | None = None
        self._seen_host: str | None = None
        self._seen_at = 0.0
        self._hint: str | None = None
        self._rtt_ms: float | None = None
        self._last_seen = 0.0
        self._since = 0.0
        self._peer_state: dict = {}
        self._last_error = ""
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, Handler] = {"echo": self._echo}

    # ── public ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def set_hint(self, host: str) -> None:
        if host and host != self._hint:
            log.info("peer hint from the VPS: %s", host)
        self._hint = host or None

    def on(self, kind: str, fn: Handler) -> None:
        self.handlers[kind] = fn

    async def request(self, kind: str, body: dict | None = None,
                      timeout: float = 10.0) -> dict:
        conn = self._conn
        if conn is None:
            return {"ok": False, "error": "peer not connected"}
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await conn.send({"type": "msg", "id": mid, "kind": kind,
                             "body": body or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "peer timeout"}
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        finally:
            self._pending.pop(mid, None)

    def status(self) -> dict:
        now = time.time()
        conn = self._conn
        return {
            "enabled": True,
            "device": self.device_id,
            "peer_device": self.peer_id,
            "connected": conn is not None,
            "role": "dialer" if self.dialer else "listener",
            "host": conn.host if conn else self._target(),
            "rtt_ms": self._rtt_ms if conn else None,
            "last_seen_s": round(now - self._last_seen, 1) if self._last_seen else None,
            "up_s": round(now - self._since) if conn else None,
            "lan_ip": lan_ip(),
            "fp": self.fp[:16],
            "pinned": bool(self._peer_fp),
            "peer_state": self._peer_state if conn else {},
            "error": "" if conn else self._last_error,
        }

    # ── run ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.fp = await asyncio.to_thread(ensure_cert, self.device_id)
        log.info("peer link: device %s (%s), fp %s…, peer fp %s",
                 self.device_id, "dialer" if self.dialer else "listener",
                 self.fp[:16], "pinned" if self._peer_fp else "NOT SET")
        jobs = [self._beacon_send(), self._beacon_listen()]
        jobs.append(self._dial_loop() if self.dialer else self._listen())
        await asyncio.gather(*jobs)

    def _target(self) -> str | None:
        if self._seen_host and time.time() - self._seen_at <= BEACON_FRESH_S:
            return self._seen_host
        return self._hint or self._static or self._seen_host

    # ── discovery ─────────────────────────────────────────────────────────

    async def _beacon_send(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        msg = json.dumps({"localvps": "peer", "device": self.device_id,
                          "port": self.port}).encode()
        while True:
            for dst in ("255.255.255.255", "192.168.0.255"):
                try:
                    sock.sendto(msg, (dst, self.beacon_port))
                except OSError:
                    pass
            await asyncio.sleep(BEACON_S)

    async def _beacon_listen(self) -> None:
        link = self

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    m = json.loads(data)
                except Exception:                    # noqa: BLE001
                    return
                if m.get("localvps") != "peer" or m.get("device") != link.peer_id:
                    return
                if link._seen_host != addr[0]:
                    log.info("peer beacon: device %s at %s", link.peer_id, addr[0])
                link._seen_host = addr[0]
                link._seen_at = time.time()

        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.create_datagram_endpoint(
                    _Proto, local_addr=("0.0.0.0", self.beacon_port),
                    allow_broadcast=True)
                return
            except OSError as exc:
                log.warning("peer beacon listen failed: %s", exc)
                await asyncio.sleep(30)

    # ── dialer ────────────────────────────────────────────────────────────

    async def _dial_loop(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        while True:
            host = self._target()
            if not self._token:
                self._last_error = "CLUSTER_TOKEN not set"
            elif not self._peer_fp:
                self._last_error = "PEER_FINGERPRINT not set"
            elif not host:
                self._last_error = "peer address unknown"
            else:
                await self._dial_once(ctx, host)
            await asyncio.sleep(DIAL_GAP_S)

    async def _dial_once(self, ctx: ssl.SSLContext, host: str) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self.port, ssl=ctx), 8)
        except Exception as exc:                     # noqa: BLE001
            self._last_error = f"dial {host}: {exc}"
            return
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        got = hashlib.sha256(der or b"").hexdigest()
        conn = _Conn(reader, writer, "dialer", host)
        if not hmac.compare_digest(got, self._peer_fp):
            self._last_error = f"fingerprint mismatch at {host} ({got[:16]}…)"
            log.warning("peer: %s", self._last_error)
            conn.close()
            return
        await self._session(conn)

    # ── listener ──────────────────────────────────────────────────────────

    async def _listen(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(CERT), str(KEY))
        while True:
            try:
                server = await asyncio.start_server(
                    self._accept, "0.0.0.0", self.port, ssl=ctx)
                break
            except OSError as exc:
                self._last_error = f"listen :{self.port}: {exc}"
                log.warning("peer: %s", self._last_error)
                await asyncio.sleep(30)
        if not self._token:
            self._last_error = "CLUSTER_TOKEN not set"
        async with server:
            await server.serve_forever()

    async def _accept(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        host = (writer.get_extra_info("peername") or ("?",))[0]
        if not self._token:
            writer.close()
            return
        await self._session(_Conn(reader, writer, "listener", host))

    # ── session ───────────────────────────────────────────────────────────

    async def _session(self, conn: _Conn) -> None:
        try:
            await conn.send({"type": "hello", "device": self.device_id,
                             "token": self._token})
            hello = await asyncio.wait_for(conn.recv(), 8)
            if (hello.get("type") != "hello"
                    or hello.get("device") != self.peer_id
                    or not hmac.compare_digest(str(hello.get("token", "")),
                                               self._token)):
                self._last_error = f"bad hello from {conn.host}"
                log.warning("peer: %s", self._last_error)
                conn.close()
                return
        except Exception as exc:                     # noqa: BLE001
            self._last_error = f"hello {conn.host}: {exc}"
            conn.close()
            return

        old, self._conn = self._conn, conn
        if old is not None:
            old.close()
        conn.peer_device = self.peer_id
        self._since = self._last_seen = time.time()
        self._last_error = ""
        log.info("peer link up: device %s at %s (%s)", self.peer_id, conn.host,
                 conn.role)
        beat = asyncio.create_task(self._beat(conn))
        try:
            while True:
                frame = await asyncio.wait_for(conn.recv(), DEAD_S)
                self._last_seen = time.time()
                await self._on_frame(conn, frame)
        except asyncio.TimeoutError:
            self._last_error = f"no frame for {DEAD_S:.0f}s"
        except Exception as exc:                     # noqa: BLE001
            self._last_error = str(exc) or type(exc).__name__
        finally:
            beat.cancel()
            conn.close()
            if self._conn is conn:
                self._conn = None
                self._rtt_ms = None
                log.info("peer link down: %s", self._last_error)
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result({"ok": False, "error": "peer link down"})

    async def _beat(self, conn: _Conn) -> None:
        while True:
            try:
                state = self._state()
            except Exception:                        # noqa: BLE001
                state = {}
            try:
                await conn.send({"type": "beat", "t": time.time(), "state": state})
            except Exception:                        # noqa: BLE001
                return
            await asyncio.sleep(BEAT_S)

    async def _on_frame(self, conn: _Conn, f: dict) -> None:
        kind = f.get("type")
        if kind == "beat":
            self._peer_state = f.get("state") or {}
            await conn.send({"type": "beat_ack", "t": f.get("t")})
        elif kind == "beat_ack":
            t = f.get("t")
            if isinstance(t, (int, float)):
                self._rtt_ms = round((time.time() - t) * 1000, 1)
        elif kind == "msg":
            asyncio.create_task(self._serve(conn, f))
        elif kind == "reply":
            fut = self._pending.get(f.get("id"))
            if fut and not fut.done():
                fut.set_result({"ok": bool(f.get("ok")), **(f.get("body") or {})})

    async def _serve(self, conn: _Conn, f: dict) -> None:
        fn = self.handlers.get(str(f.get("kind")))
        if fn is None:
            ok, body = False, {"error": f"unknown kind {f.get('kind')}"}
        else:
            try:
                body = await fn(f.get("body") or {})
                ok = bool(body.pop("ok", True))
            except Exception as exc:                 # noqa: BLE001
                ok, body = False, {"error": str(exc)}
        try:
            await conn.send({"type": "reply", "id": f.get("id"), "ok": ok,
                             "body": body})
        except Exception:                            # noqa: BLE001
            pass

    async def _echo(self, body: dict) -> dict:
        return {"ok": True, "echo": body, "device": self.device_id}


def from_env(device_id: int, state: Callable[[], dict] | None = None) -> PeerLink:
    return PeerLink(
        device_id,
        token=os.environ.get("CLUSTER_TOKEN", "").strip(),
        peer_fp=os.environ.get("PEER_FINGERPRINT", "").strip(),
        port=int(os.environ.get("PEER_PORT", "8710") or 8710),
        beacon_port=int(os.environ.get("PEER_BEACON_PORT", "8712") or 8712),
        static_host=os.environ.get("PEER_HOST", "").strip(),
        state=state,
    )
