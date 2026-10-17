"""tlsutil.py — TLS for a gate addressed by IP (no domain, no CA).

The gate serves a self-signed certificate. Instead of CA verification (which
cannot work for a bare IP + self-signed cert) the agent pins the certificate's
SHA-256 fingerprint from .env (GATE_TLS_FINGERPRINT). With no pin configured
the connection is still encrypted, just not authenticated — set the pin.
"""
from __future__ import annotations

import hashlib
import logging
import ssl

log = logging.getLogger("tlsutil")


def client_context() -> ssl.SSLContext:
    """Encrypted, no CA verification (self-signed cert on a bare IP)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def cert_fingerprint(der_cert: bytes) -> str:
    return hashlib.sha256(der_cert).hexdigest()


def check_pin(ssl_object, expected_fp: str, what: str) -> None:
    """Raise if the peer cert does not match the pinned fingerprint.

    ssl_object is an ssl.SSLObject/SSLSocket from the established connection.
    A missing pin is allowed (logged once at connect) so first install can
    read the fingerprint off the log and pin it.
    """
    if not expected_fp:
        return
    der = ssl_object.getpeercert(binary_form=True)
    if der is None:
        raise ssl.SSLError(f"{what}: no peer certificate to pin against")
    got = cert_fingerprint(der)
    if got != expected_fp.lower():
        raise ssl.SSLError(
            f"{what}: gate certificate fingerprint mismatch "
            f"(got {got}, pinned {expected_fp}) — refusing to talk")
