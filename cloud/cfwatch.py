"""cfwatch.py — keep the gate's DNS records right, through the Cloudflare API.

Every CFWATCH_EVERY_S (10 min) for gate.example.com and tasks.example.com:
  - the A record exists, points to the VPS and has the expected proxy mode
    (grey now; orange once CFWATCH_PROXIED=true in phase 5b). A missing or
    changed record is repaired through the API and an alert goes out;
  - the public resolvers (1.1.1.1, 8.8.8.8) answer: grey -> the VPS's IP,
    proxied -> Cloudflare addresses only (the VPS's IP must NOT show).
If the API itself fails, one alert goes out (the devices keep working on the
IP fallback) and one more when it works again. The token file is the limited
example.com token (dns_cloudflare_api_token = ...), never the Global key.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("cfwatch")

API = "https://api.cloudflare.com/client/v4"
RESOLVERS = ("1.1.1.1", "8.8.8.8")


def read_token(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            key, _, val = line.partition("=")
            if key.strip() == "dns_cloudflare_api_token":
                return val.strip()
    return ""


async def dig(name: str, server: str, timeout: float = 8.0) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "dig", "+short", "+time=3", "+tries=1", "A", name, f"@{server}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    return [ln.strip() for ln in out.decode().splitlines()
            if ln.strip() and ln.strip()[0].isdigit()]


class CfWatch:
    def __init__(self, notify: Callable[[str], Awaitable[None]], token_file: str,
                 zone: str, names: list[str], ip: str, proxied: bool = False,
                 every: float = 600.0, resolve: Callable = dig,
                 client: httpx.AsyncClient | None = None) -> None:
        self.notify = notify
        self.token_file = token_file
        self.zone = zone
        self.names = names
        self.ip = ip
        self.proxied = proxied
        self.every = every
        self.resolve = resolve
        self._client = client
        self._zone_id = ""
        self._api_down = False
        self._dns_bad: set[str] = set()
        self.state: dict = {"ok": None, "checked_at": None, "records": {},
                            "resolvers": {}, "error": "", "proxied": proxied}

    async def run(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.check_once()
            except Exception as exc:                 # noqa: BLE001
                log.warning("cfwatch check failed: %s", exc)
            await asyncio.sleep(self.every)

    def snapshot(self) -> dict:
        return dict(self.state)

    # ── one pass ─────────────────────────────────────────────────────────
    async def check_once(self) -> dict:
        records: dict = {}
        error = ""
        try:
            records = await self._check_records()
            if self._api_down:
                self._api_down = False
                await self.notify("✅ Cloudflare API works again (DNS watch).")
        except Exception as exc:                     # noqa: BLE001
            error = f"Cloudflare API: {exc}"[:300]
            log.warning(error)
            if not self._api_down:
                self._api_down = True
                await self.notify(f"⚠️ DNS watch: {error} — devices stay on the IP "
                                  "fallback if the domain breaks.")
        resolvers = await self._check_resolvers()
        ok = (not error and all(r.get("ok") for r in records.values())
              and all(r.get("ok") for r in resolvers.values()))
        self.state = {"ok": ok, "checked_at": time.time(), "records": records,
                      "resolvers": resolvers, "error": error, "proxied": self.proxied}
        return self.state

    async def _api(self, method: str, path: str, **kw) -> dict:
        token = read_token(self.token_file)
        if not token:
            raise RuntimeError(f"no token in {self.token_file}")
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            r = await client.request(method, API + path, **kw,
                                     headers={"Authorization": f"Bearer {token}"})
        finally:
            if self._client is None:
                await client.aclose()
        body = r.json()
        if not body.get("success"):
            errs = "; ".join(e.get("message", "?") for e in body.get("errors") or [])
            raise RuntimeError(f"{method} {path.split('?')[0]} -> HTTP {r.status_code} {errs}")
        return body

    async def _check_records(self) -> dict:
        if not self._zone_id:
            zones = (await self._api("GET", f"/zones?name={self.zone}"))["result"]
            if not zones:
                raise RuntimeError(f"zone {self.zone} not visible to the token")
            self._zone_id = zones[0]["id"]
        base = f"/zones/{self._zone_id}/dns_records"
        out: dict = {}
        for name in self.names:
            found = (await self._api("GET", f"{base}?type=A&name={name}"))["result"]
            want = {"type": "A", "name": name, "content": self.ip,
                    "proxied": self.proxied, "ttl": 1 if self.proxied else 300}
            if not found:
                await self._api("POST", base, json=want)
                out[name] = {"ok": True, "repaired": "created"}
                await self.notify(f"🛠 DNS watch: {name} was missing — created it "
                                  f"({'proxied' if self.proxied else 'DNS only'}).")
                continue
            rec = found[0]
            wrong = []
            if rec.get("content") != self.ip:
                wrong.append(f"pointed to {rec.get('content')}")
            if bool(rec.get("proxied")) != self.proxied:
                wrong.append("proxied" if rec.get("proxied") else "DNS only")
            if wrong:
                await self._api("PATCH", f"{base}/{rec['id']}",
                                json={"content": self.ip, "proxied": self.proxied})
                out[name] = {"ok": True, "repaired": ", ".join(wrong)}
                await self.notify(f"🛠 DNS watch: {name} was changed "
                                  f"({', '.join(wrong)}) — repaired.")
            else:
                out[name] = {"ok": True, "proxied": bool(rec.get("proxied"))}
            if len(found) > 1:
                out[name]["extra_records"] = len(found) - 1
        return out

    async def _check_resolvers(self) -> dict:
        out: dict = {}
        for name in self.names:
            for server in RESOLVERS:
                ips = await self.resolve(name, server)
                if self.proxied:
                    ok = bool(ips) and self.ip not in ips
                else:
                    ok = self.ip in ips
                key = f"{name}@{server}"
                out[key] = {"ok": ok, "ips": ips}
                if not ok and key not in self._dns_bad:
                    self._dns_bad.add(key)
                    want = "Cloudflare addresses only" if self.proxied else self.ip
                    await self.notify(f"⚠️ DNS watch: {server} answers {name} with "
                                      f"{', '.join(ips) or 'nothing'} (expected {want}).")
                elif ok and key in self._dns_bad:
                    self._dns_bad.discard(key)
                    await self.notify(f"✅ DNS watch: {server} answers {name} correctly again.")
        return out

    def status_line(self) -> str:
        s = self.state
        if s.get("ok") is None:
            return "DNS watch: not checked yet"
        mode = "proxied" if self.proxied else "DNS only"
        if s.get("ok"):
            mins = int((time.time() - (s.get("checked_at") or time.time())) / 60)
            return f"DNS watch: ok · {', '.join(self.names)} ({mode}) · checked {mins} min ago"
        bad = [k for k, v in {**s.get("records", {}), **s.get("resolvers", {})}.items()
               if not v.get("ok")]
        return "DNS watch: ⚠️ " + (s.get("error") or "bad: " + ", ".join(bad))
