"""Gate configuration loaded from .env (searched in this folder and the parent folder)."""
import os
from pathlib import Path

from dotenv import load_dotenv

_here = Path(__file__).resolve().parent
for candidate in (_here / ".env", _here.parent / ".env"):
    if candidate.exists():
        load_dotenv(candidate)
        break

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "change-me")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8080"))

# Callback delivery retries
CALLBACK_RETRIES = int(os.getenv("CALLBACK_RETRIES", "3"))
CALLBACK_RETRY_DELAY = float(os.getenv("CALLBACK_RETRY_DELAY", "2"))
CALLBACK_TIMEOUT = float(os.getenv("CALLBACK_TIMEOUT", "15"))

# Telegram bot (optional - leave TELEGRAM_BOT_TOKEN empty to disable)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Backup extension runner: shared password the extension sends as X-Ext-Auth
EXT_AUTH = os.getenv("EXT_AUTH", "change-me-ext-password")
# Manual-console token the admin_ext extension sends as X-Auth-Token on the
# /jobs + /jobs/{id}/code compatibility routes. Set it to the SAME value the
# extension already uses for the backend manual queue (MANUAL_EXT_TOKEN) so
# one token covers both services. Falls back to EXT_AUTH when unset.
CONSOLE_TOKEN = os.getenv("CONSOLE_TOKEN", "").strip() or EXT_AUTH
# How long the extension has to answer a backup task before the gate gives up
# and delivers the last HP error/timeout to the callback_url (seconds).
EXT_TASK_TTL = float(os.getenv("EXT_TASK_TTL", "1800"))

# Admin HTTP API key. The backend (loopback, same VPS) sends it as the
# X-Admin-Key header to read status / run test tasks / control browsers.
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me-admin-key")

# How many finished tasks to keep for the admin task history.
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "100"))

# Local VPS power gate (night idle shutdown / wake-on-task). Loopback on the
# same VPS box. Leave POWER_GATE_URL empty to disable the integration.
# POWER_GATE_KEY = the power gate's ADMIN_KEY.
POWER_GATE_URL = os.getenv("POWER_GATE_URL", "").strip().rstrip("/")
POWER_GATE_KEY = os.getenv("POWER_GATE_KEY", "").strip()
