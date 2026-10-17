"""Code version of this agent: a short hash of its .py files, so the VPS and the
peer can tell when the two devices run different code."""
from __future__ import annotations

import hashlib
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def code_hash(folder: Path = _HERE) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.glob("*.py")):
        if p.name.startswith("test_"):
            continue
        h.update(p.name.encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


VERSION = code_hash()
