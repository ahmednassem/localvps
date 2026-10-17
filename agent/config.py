"""config.py — load the agent .env into a typed Config object.

Used by every agent module. Because the service runs as LocalSystem the .env is
read from next to this file (the install dir), not a user home.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> None:
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# The gate is reached on GATE_DOMAIN first (real CA certificate), with the bare
# GATE_IP as the permanent fallback (self-signed cert, pinned by its SHA-256
# fingerprint). See gatelink.py for when the agent switches between them.
GATE_PORT_DEFAULT = "443"


@dataclass
class Config:
    # identity: which device this agent is (device 1 or device 2)
    device_id: int = 1
    # tunnels (control + status only; no data/connmgr)
    control_url: str = ""
    status_url: str = ""
    control_url_domain: str = ""
    status_url_domain: str = ""
    agent_token: str = ""
    # gate TLS: self-signed cert on the gate, pinned by fingerprint here
    gate_tls: bool = True
    gate_tls_fingerprint: str = ""     # sha256 hex of the gate cert (optional pin)
    # timing
    status_interval: float = 60.0
    reconnect_min: float = 2.0
    reconnect_max: float = 30.0
    # kill switch
    max_temp_c: float = 90.0
    temp_grace: float = 30.0
    # networks (SSIDs) and devices (PnP friendly-name substrings)
    wifi_dsl_ssid: str = ""
    wifi_sms_ssid: str = ""
    eth_adapter: str = ""
    fan_device: str = ""
    sms_device: str = ""
    # paths
    anydesk_path: str = r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe"
    log_dir: str = field(default_factory=lambda: os.path.join(HERE, "logs"))
    host_name: str = ""
    # display / backlight
    dim_backlight_on_start: bool = True
    backlight_dim_level: int = 0
    restore_brightness_pct: int = 70
    disconnect_alert_sec: float = 45.0
    # network auto-failover
    net_watch_enabled: bool = True
    net_watch_interval: float = 30.0
    net_check_host: str = "1.1.1.1"
    net_check_port: int = 53
    net_check_timeout: float = 4.0
    net_fail_threshold: int = 3
    net_switch_cooldown: float = 120.0
    net_post_switch_wait: float = 15.0
    net_auto_discover: bool = True
    # second network (Wi‑Fi DSL) warm check
    net_second_check_enabled: bool = True
    net_second_check_interval: float = 600.0   # 10 minutes
    # SMS Wi‑Fi keep-alive (SMS device sharing sleeps when idle/charging)
    sms_keepalive_enabled: bool = True
    sms_keepalive_interval: float = 180.0     # touch every 3 min
    sms_keepalive_hold_sec: float = 20.0      # stay on SMS Wi‑Fi this long
    # AC / battery
    power_watch_enabled: bool = True
    power_watch_interval: float = 15.0
    # anydesk
    anydesk_id: str = ""
    anydesk_watch_enabled: bool = True
    anydesk_watch_interval: float = 60.0
    # rustdesk (self-hosted, runs alongside anydesk)
    rustdesk_path: str = r"C:\Users\<you>\AppData\Local\rustdesk\rustdesk.exe"
    rustdesk_id: str = ""
    rustdesk_watch_enabled: bool = True
    rustdesk_watch_interval: float = 60.0
    # network boxes we can reboot to force a new public IP
    router_dsl_host: str = "192.168.0.1"
    router_dsl_user: str = ""
    router_dsl_pass: str = ""
    router_sms_host: str = "192.168.0.254"
    router_sms_user: str = ""
    router_sms_pass: str = ""
    router_reboot_wait: float = 120.0
    router_reboot_min_interval: float = 1800.0
    # the third box: the NEW router serving the cable eth uplink (own WAN).
    # Reboot support is added once its admin UI is discovered at deploy time.
    router_eth_host: str = ""
    router_eth_user: str = ""
    router_eth_pass: str = ""

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv()
        def _bool(key: str, default: str = "true") -> bool:
            return _env(key, default).lower() in ("1", "true", "yes", "on")

        gate_ip = _env("GATE_IP")
        gate_port = _env("GATE_PORT", GATE_PORT_DEFAULT)
        gate_tls = _bool("GATE_TLS", "true")
        ws_scheme = "wss" if gate_tls else "ws"
        http_scheme = "https" if gate_tls else "http"
        control = _env("CONTROL_URL") or (
            f"{ws_scheme}://{gate_ip}:{gate_port}/agent/ws" if gate_ip else "")
        status = _env("STATUS_URL") or (
            f"{http_scheme}://{gate_ip}:{gate_port}/status" if gate_ip else "")
        gate_domain = _env("GATE_DOMAIN") if gate_tls else ""

        return cls(
            device_id=int(_env("DEVICE_ID", "1") or "1"),
            control_url=control,
            status_url=status,
            control_url_domain=f"wss://{gate_domain}/agent/ws" if gate_domain else "",
            status_url_domain=f"https://{gate_domain}/status" if gate_domain else "",
            agent_token=_env("AGENT_TOKEN"),
            gate_tls=gate_tls,
            gate_tls_fingerprint=_env("GATE_TLS_FINGERPRINT").replace(":", "").lower(),
            status_interval=float(_env("STATUS_INTERVAL", "60") or "60"),
            reconnect_min=float(_env("RECONNECT_MIN", "2") or "2"),
            reconnect_max=float(_env("RECONNECT_MAX", "30") or "30"),
            max_temp_c=float(_env("MAX_TEMP_C", "90") or "90"),
            temp_grace=float(_env("TEMP_GRACE", "30") or "30"),
            wifi_dsl_ssid=_env("WIFI_DSL_SSID"),
            wifi_sms_ssid=_env("WIFI_SMS_SSID"),
            eth_adapter=_env("ETH_ADAPTER"),
            fan_device=_env("FAN_DEVICE"),
            sms_device=_env("SMS_DEVICE"),
            anydesk_path=_env("ANYDESK_PATH",
                              r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe"),
            log_dir=_env("LOG_DIR", os.path.join(HERE, "logs")),
            host_name=_env("HOST_NAME", os.environ.get("COMPUTERNAME", "laptop")),
            dim_backlight_on_start=_bool("DIM_BACKLIGHT_ON_START", "true"),
            backlight_dim_level=int(_env("BACKLIGHT_DIM_LEVEL", "0") or "0"),
            restore_brightness_pct=int(_env("RESTORE_BRIGHTNESS_PCT", "70") or "70"),
            disconnect_alert_sec=float(_env("DISCONNECT_ALERT_SEC", "45") or "45"),
            net_watch_enabled=_bool("NET_WATCH_ENABLED", "true"),
            net_watch_interval=float(_env("NET_WATCH_INTERVAL", "30") or "30"),
            net_check_host=_env("NET_CHECK_HOST", "1.1.1.1"),
            net_check_port=int(_env("NET_CHECK_PORT", "53") or "53"),
            net_check_timeout=float(_env("NET_CHECK_TIMEOUT", "4") or "4"),
            net_fail_threshold=int(_env("NET_FAIL_THRESHOLD", "3") or "3"),
            net_switch_cooldown=float(_env("NET_SWITCH_COOLDOWN", "120") or "120"),
            net_post_switch_wait=float(_env("NET_POST_SWITCH_WAIT", "15") or "15"),
            net_auto_discover=_bool("NET_AUTO_DISCOVER", "true"),
            net_second_check_enabled=_bool("NET_SECOND_CHECK_ENABLED", "true"),
            net_second_check_interval=float(
                _env("NET_SECOND_CHECK_INTERVAL", "600") or "600"),
            sms_keepalive_enabled=_bool("SMS_KEEPALIVE_ENABLED", "true"),
            sms_keepalive_interval=float(
                _env("SMS_KEEPALIVE_INTERVAL", "180") or "180"),
            sms_keepalive_hold_sec=float(
                _env("SMS_KEEPALIVE_HOLD_SEC", "20") or "20"),
            power_watch_enabled=_bool("POWER_WATCH_ENABLED", "true"),
            power_watch_interval=float(_env("POWER_WATCH_INTERVAL", "15") or "15"),
            anydesk_id=_env("ANYDESK_ID"),
            anydesk_watch_enabled=_bool("ANYDESK_WATCH_ENABLED", "true"),
            anydesk_watch_interval=float(_env("ANYDESK_WATCH_INTERVAL", "15") or "15"),
            rustdesk_path=_env(
                "RUSTDESK_PATH",
                r"C:\Users\<you>\AppData\Local\rustdesk\rustdesk.exe"),
            rustdesk_id=_env("RUSTDESK_ID"),
            rustdesk_watch_enabled=_bool("RUSTDESK_WATCH_ENABLED", "true"),
            rustdesk_watch_interval=float(
                _env("RUSTDESK_WATCH_INTERVAL", "15") or "15"),
            router_dsl_host=_env("ROUTER_DSL_HOST", "192.168.0.1"),
            router_dsl_user=_env("ROUTER_DSL_USER"),
            router_dsl_pass=_env("ROUTER_DSL_PASS"),
            router_sms_host=_env("ROUTER_SMS_HOST", "192.168.0.254"),
            router_sms_user=_env("ROUTER_SMS_USER"),
            router_sms_pass=_env("ROUTER_SMS_PASS"),
            router_reboot_wait=float(_env("ROUTER_REBOOT_WAIT", "120") or "120"),
            router_reboot_min_interval=float(
                _env("ROUTER_REBOOT_MIN_INTERVAL_S", "1800") or "1800"),
            router_eth_host=_env("ROUTER_ETH_HOST"),
            router_eth_user=_env("ROUTER_ETH_USER"),
            router_eth_pass=_env("ROUTER_ETH_PASS"),
        )
