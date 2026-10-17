"""Telegram bot for the gate.

Three jobs:
  - send(text): push a message, return its message_id (None if disabled or
    the send failed).
  - edit(message_id, text): replace that message's text. Used so each task
    keeps ONE message that is updated as it moves.
  - notify(text): fire-and-forget send for gate events (device up/down).
  - run_polling(status_provider): long-poll getUpdates and answer /status
    and /help. Only the admin chat id is honoured.

If TELEGRAM_BOT_TOKEN is empty the whole module is a no-op, so the gate runs
exactly as it would without Telegram.
"""
import asyncio
import logging

import httpx

import config

log = logging.getLogger("gate.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_enabled = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def enabled() -> bool:
    return _enabled


def _url(method: str) -> str:
    return _API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)


async def _post(method: str, payload: dict):
    """POST to the Bot API. Retries once; on HTTP 429 waits the asked time.
    Returns the parsed result, or None."""
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(_url(method), json=payload)
            data = resp.json()
            if data.get("ok"):
                return data.get("result")
            if resp.status_code == 429 and attempt < 3:
                wait = float((data.get("parameters") or {}).get("retry_after", 2))
                await asyncio.sleep(min(wait, 10))
                continue
            # editing to identical text is not a failure
            if "message is not modified" in str(data.get("description")):
                return True
            log.warning("telegram %s rejected: %s", method, data.get("description"))
            return None
        except Exception as exc:
            log.warning("telegram %s attempt %d failed: %s", method, attempt, exc)
            await asyncio.sleep(1)
    return None


async def send(text: str):
    """Send a message to the admin chat. Returns its message_id, or None."""
    if not _enabled:
        log.debug("telegram disabled, dropping send: %s", text)
        return None
    result = await _post("sendMessage", {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    })
    if isinstance(result, dict):
        return result.get("message_id")
    return None


async def edit(message_id, text: str) -> bool:
    """Replace a message's text. Returns True when the card is up to date."""
    if not _enabled or not message_id:
        return False
    result = await _post("editMessageText", {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    })
    return result is not None


async def notify(text: str) -> None:
    """Send a message to the admin chat. Swallows all errors."""
    await send(text)


def notify_bg(text: str) -> None:
    """Fire-and-forget notify from sync-ish call sites inside the event loop."""
    if not _enabled:
        return
    asyncio.create_task(notify(text))


async def run_polling(status_provider, task_lookup=None) -> None:
    """Long-poll getUpdates and answer commands. status_provider() -> str;
    task_lookup(ref) -> str answers /task <ref>.

    Runs forever; safe to launch as a background task. No-op when disabled.
    """
    if not _enabled:
        log.info("telegram bot disabled (no token/chat id) - not polling")
        return

    log.info("telegram bot polling started")
    offset = None
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                params = {"timeout": 30}
                if offset is not None:
                    params["offset"] = offset
                resp = await client.get(_url("getUpdates"), params=params)
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    await _handle_update(client, update, status_provider, task_lookup)
            except Exception as exc:
                log.warning("telegram polling error: %s", exc)
                await asyncio.sleep(3)


async def _handle_update(client, update, status_provider, task_lookup=None) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        log.info("ignoring telegram message from non-admin chat %s", chat_id)
        return

    cmd = text.split()[0].lower() if text else ""
    args = text.split()[1:]
    if cmd in ("/status", "/status@"):
        reply = status_provider()
    elif cmd == "/task" and task_lookup is not None:
        reply = task_lookup(args[0]) if args else "Usage: /task 29/12-5"
    elif cmd in ("/help", "/start"):
        reply = ("Task-relay gate\n"
                 "/status - devices, tasks in flight, backup pool\n"
                 "/task 29/12-5 - one task by its reference\n"
                 "/help - this message")
    else:
        return

    try:
        await client.post(_url("sendMessage"), json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": reply,
            "disable_web_page_preview": True,
        })
    except Exception as exc:
        log.warning("failed to reply to telegram command: %s", exc)
