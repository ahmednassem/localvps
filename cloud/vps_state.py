"""vps_state.py — persist intentional-offline flag across gate restarts."""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(_HERE, "vps_state.json")


def load(path: str = DEFAULT_PATH) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(json.load(fh).get("intentional_offline"))
    except Exception:
        return False


def save(intentional_offline: bool, path: str = DEFAULT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"intentional_offline": intentional_offline}, fh)
