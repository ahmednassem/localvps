"""main.py — run the local-server gate on the VPS in one process.

Loads .env, builds the shared Hub plus one service bundle per device
(device 1 or device 2), then starts together:
  - the FastAPI gate (uvicorn, direct IP, TLS): /agent/ws + admin/output API
  - the admin Telegram bot (polling)
  - per-device health watchdog / dead-man, night watch and net rotation
  - the scheduler and the lsvc device feed
All share the one Hub. The bot also provides the alert channel used by the
watchdogs.
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from alerts import Watchdog
from bot import GateBot
from cfwatch import CfWatch
from gate import build_app
from cluster import Cluster
from health import Health, PlugWatch
from hub import Hub
from lsvc_feed import LsvcFeed
from netrotate import BoxReboots, NetRotate
from night import NightWatch
from power import MqttLink, SmartPlug
from scheduler import JobRunner
from services import DeviceServices
from vps_power import VpsPower


def _parse_hour(value: str, default: int) -> int:
    """Accept "23" or "23:00" and return the hour."""
    value = value.strip()
    if not value:
        return default
    try:
        return int(value.split(":", 1)[0]) % 24
    except ValueError:
        return default


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


async def _run() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("main")

    bot_token = _env("BOT_TOKEN")
    admin_id = int(_env("ADMIN_USER_ID", "0") or "0")
    agent_token = _env("AGENT_TOKEN")
    admin_key = _env("ADMIN_KEY")
    out_key = _env("OUT_API_KEY")               # defaults to ADMIN_KEY in gate
    http_host = _env("HTTP_HOST", "0.0.0.0")
    http_port = int(_env("GATE_PORT", "8201"))
    ssl_certfile = _env("SSL_CERTFILE")
    ssl_keyfile = _env("SSL_KEYFILE")
    grace = float(_env("DEADMAN_GRACE", "150") or "150")
    temp_warn = float(_env("TEMP_WARN_C", "85") or "85")
    status_report = float(_env("STATUS_REPORT_INTERVAL", "1800") or "1800")
    cpu_warn = float(_env("CPU_WARN_PCT", "90") or "90")
    ram_warn = float(_env("RAM_WARN_PCT", "90") or "90")
    disk_warn = float(_env("DISK_WARN_PCT", "90") or "90")
    battery_low = float(_env("BATTERY_LOW_PCT", "20") or "20")
    schedule_path = _env("SCHEDULE_FILE") or os.path.join(
        os.path.dirname(__file__), "schedule.json")
    schedule_tz = _env("SCHEDULE_TZ", "UTC")
    night_start = _parse_hour(_env("NIGHT_START", "23"), 23)
    night_end = _parse_hour(_env("NIGHT_END", "9"), 9)
    night_idle_min = float(_env("NIGHT_IDLE_MINUTES", "30") or "30")
    lsvc_url = _env("LSVC_URL", "http://127.0.0.1:8102")
    lsvc_key = _env("LSVC_KEY")

    def _bool(key: str, default: str) -> bool:
        return _env(key, default).lower() in ("1", "true", "yes", "on")

    if not bot_token or not admin_id:
        raise SystemExit("BOT_TOKEN and ADMIN_USER_ID are required in .env")
    if not agent_token or not admin_key:
        raise SystemExit("AGENT_TOKEN and ADMIN_KEY are required")

    labels = {
        1: _env("DEVICE1_LABEL", "laptop1"),
        2: _env("DEVICE2_LABEL", "laptop2"),
    }

    hub = Hub(labels)
    services: dict[int, DeviceServices] = {}
    bot = GateBot(bot_token, admin_id, hub, services)

    home_timeout = float(_env("PLUG_HOME_TIMEOUT_S", "6") or "6")

    async def plug_via_home(n: int, op: str) -> dict | None:
        """Ask a device at home to switch plug n over the LAN: the other
        laptop first (a hung or unpowered device can't answer for itself)."""
        for d in sorted(labels, key=lambda d: d == n):
            if hub.device(d).control is None:
                continue
            try:
                r = await hub.send_command(d, "action", "plug",
                                           {"plug": n, "op": op},
                                           timeout=home_timeout)
            except Exception as exc:                # noqa: BLE001
                log.info("plug %d %s via device %d failed: %s", n, op, d, exc)
                continue
            if r.get("ok"):
                return {**r, "via": d}
            log.info("plug %d %s via device %d: %s", n, op, d, r.get("error"))
        return None

    mqtt_link = MqttLink(_env("MQTT_HOST", "127.0.0.1"),
                         int(_env("MQTT_PORT", "1883") or "1883"),
                         _env("MQTT_USER"), _env("MQTT_PASS"))

    def make_plug(did: int) -> SmartPlug:
        return SmartPlug(mqtt_link, _env(f"PLUG{did}_MQTT_TOPIC", f"plug{did}"),
                         number=did, home=plug_via_home,
                         relay_ch=int(_env(f"PLUG{did}_RELAY_CH", "1") or "1"),
                         lamp_ch=int(_env(f"PLUG{did}_LAMP_CH", "2") or "2"),
                         min_watts=float(_env("PLUG_MIN_WATTS", "5") or "5"),
                         cycle_wait=float(_env("PLUG_CYCLE_WAIT", "10") or "10"),
                         wifi_main=_env("PLUG_WIFI_MAIN"),
                         wifi_backup=_env("PLUG_WIFI_BACKUP"))

    box_reboots = BoxReboots(
        bot.notify_admin,
        every=int(_env("NETROTATE_REBOOT_EVERY", "4") or "4"),
        reboot={"dsl": _bool("NETROTATE_REBOOT_DSL", "false"),
                "sms": _bool("NETROTATE_REBOOT_SMS", "false")})
    net_boxes = dict(pair.split(":", 1) for pair in
                     (_env("NETROTATE_BOXES", "wifi:dsl,eth:dsl,sms:sms") or "").split(",")
                     if ":" in pair)
    default_paths = {1: "wifi,sms", 2: "eth,sms"}
    for did, label in labels.items():
        plug = make_plug(did)
        vps = VpsPower(
            hub, did, plug, bot.notify_admin,
            cut_after_shutdown=_bool("PLUG_CUT_AFTER_SHUTDOWN", "true"),
            cut_settle_s=float(_env("PLUG_CUT_SETTLE_S", "60") or "60"),
            cut_max_wait_s=float(_env("PLUG_CUT_MAX_WAIT_S", "300") or "300"),
        )
        watchdog = Watchdog(
            hub, did, plug, bot.notify_admin,
            grace=grace, temp_warn_c=temp_warn,
            status_report_interval=status_report,
            cpu_warn=cpu_warn, ram_warn=ram_warn, disk_warn=disk_warn,
            battery_low=battery_low,
            # one scheduled digest covers all devices — attach it once
            status_digest=bot.send_status_digest if did == 1 else None,
        )
        night = NightWatch(hub, did, vps, bot.notify_admin,
                           tz=schedule_tz,
                           start_hour=night_start, end_hour=night_end,
                           idle_minutes=night_idle_min,
                           always=_bool("NIGHT_ALWAYS", "false"))
        netrotate = NetRotate(
            hub, did, bot.notify_admin,
            enabled=_bool("NETROTATE_ENABLED", "true"),
            every_tasks=int(_env("NETROTATE_EVERY_TASKS", "20") or "20"),
            reject_streak=int(_env("NETROTATE_REJECT_STREAK", "3") or "3"),
            report_every=int(_env("NETROTATE_REPORT_EVERY", "100") or "100"),
            dead_penalty_tasks=int(_env("NETROTATE_DEAD_PENALTY_TASKS", "50") or "50"),
            cooldown_s=float(_env("NETROTATE_COOLDOWN_S", "300") or "300"),
            max_defer_s=float(_env("NETROTATE_MAX_DEFER_S", "300") or "300"),
            reboot_dsl_on_leave=_bool("NETROTATE_REBOOT_DSL", "true"),
            reboot_eth_on_leave=_bool("NETROTATE_REBOOT_ETH", "false"),
            reboot_sms_on_leave=_bool("NETROTATE_REBOOT_SMS", "false"),
            paths=tuple((_env(f"NETROTATE_PATHS_{did}", default_paths.get(did, "wifi,sms"))
                         or "wifi,sms").split(",")),
            boxes=net_boxes,
            sms_check_s=float(_env("NETROTATE_SMS_CHECK_S", "45") or "45"),
            sms_bad_s=float(_env("NETROTATE_SMS_BAD_S", "600") or "600"),
            reboots=box_reboots,
        )
        services[did] = DeviceServices(plug=plug, vps=vps, night=night,
                                       netrotate=netrotate, watchdog=watchdog)
        log.info("device %d (%s): plug_configured=%s", did, label, plug.configured)

    hub.on_event = bot.notify_event

    plugwatch = PlugWatch({d: s.plug for d, s in services.items()},
                          poll=float(_env("PLUG_POLL_S", "60") or "60"))
    health = Health(hub, services, plugwatch, bot.notify_admin,
                    act=_bool("HEALTH_ACT", "true"))
    cluster = Cluster(hub, services, health, notify=bot.notify_admin)
    hub.cluster = cluster
    hub.health = health
    cf_token = _env("CFWATCH_TOKEN_FILE", "/root/.secrets/cloudflare.ini")
    cfw = None
    if _bool("CFWATCH_ENABLED", "true") and os.path.exists(cf_token):
        cfw = CfWatch(bot.notify_admin, cf_token,
                      zone=_env("CFWATCH_ZONE", "example.com"),
                      names=[n.strip() for n in _env(
                          "CFWATCH_NAMES", "gate.example.com,tasks.example.com").split(",")
                          if n.strip()],
                      ip=_env("CFWATCH_IP", "203.0.113.10"),
                      proxied=_bool("CFWATCH_PROXIED", "false"),
                      every=float(_env("CFWATCH_EVERY_S", "600") or "600"))
    hub.cfwatch = cfw

    scheduler = JobRunner(hub, services, bot.notify_admin, schedule_path,
                          schedule_tz=schedule_tz)
    feed = LsvcFeed(hub, lsvc_url, lsvc_key)
    hub.on_device_change = feed.on_device_change

    app = build_app(hub, services, agent_token=agent_token,
                    admin_key=admin_key, out_key=out_key)

    config = uvicorn.Config(
        app, host=http_host, port=http_port,
        log_level=_env("LOG_LEVEL", "info").lower(),
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
    )
    server = uvicorn.Server(config)

    mqtt_link.start()

    # start the telegram bot (polling) without blocking
    await bot.app.initialize()
    await bot.app.start()
    await bot.app.updater.start_polling()

    tasks = [asyncio.create_task(scheduler.run()),
             asyncio.create_task(feed.run()),
             asyncio.create_task(plugwatch.run()),
             asyncio.create_task(health.run()),
             asyncio.create_task(cluster.run())]
    if cfw is not None:
        tasks.append(asyncio.create_task(cfw.run()))
    for svc in services.values():
        tasks.append(asyncio.create_task(svc.watchdog.run()))
        tasks.append(asyncio.create_task(svc.night.run()))
        tasks.append(asyncio.create_task(svc.netrotate.run()))
    log.info("local server gate running (http%s %s:%d)",
             "s" if ssl_certfile else "", http_host, http_port)
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        mqtt_link.stop()
        await bot.app.updater.stop()
        await bot.app.stop()
        await bot.app.shutdown()


if __name__ == "__main__":
    _load_dotenv()
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass
