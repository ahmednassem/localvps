"""display_app.py — modern always-on Local VPS status GUI (user session).

Live cards + radio network picker (Wi‑Fi / Ethernet / SMS — one active).
Space×3 brings window to front. Managed as supervisor unit session=user.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

from config import Config
import status_store

log = logging.getLogger("display")

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
VK_SPACE = 0x20
VK_F2 = 0x71
HC_ACTION = 0

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Palette — deep slate + teal accent (not generic purple)
BG = "#0b1220"
PANEL = "#121a2b"
PANEL2 = "#182338"
BORDER = "#243049"
TEXT = "#e8eef9"
MUTED = "#8b9bb8"
ACCENT = "#2dd4bf"
ACCENT_DIM = "#0f766e"
OK = "#34d399"
WARN = "#fbbf24"
BAD = "#f87171"
BLUE = "#60a5fa"


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


LowLevelKeyboardProc = ctypes.CFUNCTYPE(
    ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(KBDLLHOOKSTRUCT)
)


def _pill(parent, text, bg, fg=TEXT, **pack) -> tk.Label:
    lbl = tk.Label(
        parent, text=text, bg=bg, fg=fg,
        font=("Segoe UI Semibold", 10), padx=10, pady=4,
    )
    lbl.pack(**pack)
    return lbl


class MetricCard(tk.Frame):
    def __init__(self, parent, title: str, **kw):
        super().__init__(parent, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1, **kw)
        self.title = tk.Label(
            self, text=title.upper(), bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self.title.pack(fill="x", padx=14, pady=(12, 2))
        self.value = tk.Label(
            self, text="—", bg=PANEL, fg=TEXT,
            font=("Segoe UI Semibold", 18), anchor="w",
        )
        self.value.pack(fill="x", padx=14, pady=(0, 2))
        self.sub = tk.Label(
            self, text="", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )
        self.sub.pack(fill="x", padx=14, pady=(0, 12))
        self._bar_bg = tk.Frame(self, bg=PANEL2, height=6)
        self._bar_bg.pack(fill="x", padx=14, pady=(0, 14))
        self._bar_bg.pack_propagate(False)
        self._bar = tk.Frame(self._bar_bg, bg=ACCENT, height=6)
        self._bar.place(x=0, y=0, relheight=1, relwidth=0)

    def set(self, value: str, sub: str = "", pct: float | None = None,
            tone: str = "ok") -> None:
        color = {"ok": OK, "warn": WARN, "bad": BAD, "info": BLUE,
                 "accent": ACCENT}.get(tone, TEXT)
        self.value.config(text=value, fg=color)
        self.sub.config(text=sub)
        if pct is None:
            self._bar.place_forget()
        else:
            self._bar.configure(bg=color)
            self._bar.place(x=0, y=0, relheight=1,
                            relwidth=max(0.0, min(1.0, pct / 100.0)))


class DisplayApp:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.status_path = status_store.path_for(self.cfg)
        os.makedirs(self.cfg.log_dir, exist_ok=True)

        self._space_count = 0
        self._last_space = 0.0
        self._space_window = 1.5
        self._error_mode = False
        self._last_alert_at: float | None = None
        self._hook_cb = None
        self._busy = False
        self._pulse = 0
        self._hold_net: str | None = None
        self._hold_net_until: float = 0.0
        self._last_net_switch_at: float | None = None

        self.root = tk.Tk()
        self.root.title("Local VPS")
        self.root.configure(bg=BG)
        self.root.geometry("980x720")
        self.root.minsize(820, 600)
        self.root.attributes("-topmost", False)
        self.root.protocol("WM_DELETE_WINDOW", self._minimize)

        self._build_ui()
        self.root.bind("<F2>", lambda _e: None)  # setup UI removed

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x", padx=20, pady=(16, 8))

        left = tk.Frame(hdr, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(
            left, text="LOCAL VPS", bg=BG, fg=ACCENT,
            font=("Segoe UI Black", 22),
        ).pack(anchor="w")
        self._host_lbl = tk.Label(
            left, text="—", bg=BG, fg=MUTED, font=("Segoe UI", 10),
        )
        self._host_lbl.pack(anchor="w")

        right = tk.Frame(hdr, bg=BG)
        right.pack(side="right")
        self._live_dot = tk.Canvas(right, width=12, height=12, bg=BG,
                                   highlightthickness=0)
        self._live_dot.pack(side="left", padx=(0, 8))
        self._live_oval = self._live_dot.create_oval(2, 2, 10, 10, fill=OK, outline="")
        self._live_lbl = tk.Label(
            right, text="LIVE", bg=BG, fg=OK,
            font=("Segoe UI Semibold", 11),
        )
        self._live_lbl.pack(side="left")

        # Alert banner
        self._err_banner = tk.Label(
            self.root, text="", font=("Segoe UI Semibold", 12),
            fg="#fecaca", bg="#3f1212", wraplength=900, justify="left",
            padx=16, pady=12,
        )

        # Cards grid
        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill="both", expand=True, padx=20, pady=8)
        for i in range(2):
            grid.columnconfigure(i, weight=1, uniform="c")
        for i in range(2):
            grid.rowconfigure(i, weight=1, uniform="r")

        self.card_link = MetricCard(grid, "Gate link")
        self.card_power = MetricCard(grid, "Power")
        self.card_net = MetricCard(grid, "Network")
        self.card_sys = MetricCard(grid, "System")
        self.card_link.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.card_power.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))
        self.card_net.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))
        self.card_sys.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))

        # Action bar — choose one network path
        actions = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER,
                           highlightthickness=1)
        actions.pack(fill="x", padx=20, pady=(8, 6))
        tk.Label(
            actions, text="ACTIVE NETWORK  (pick one)", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        row = tk.Frame(actions, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(0, 6))

        self._net_choice = tk.StringVar(value="")
        self._radio = {}
        for key, label in (
            ("wifi", "Wi‑Fi (DSL)"),
            ("eth", "Ethernet"),
            ("sms", "SMS hotspot"),
        ):
            rb = tk.Radiobutton(
                row, text=label, value=key, variable=self._net_choice,
                command=lambda k=key: self._on_net_radio(k),
                bg=PANEL, fg=TEXT, selectcolor=PANEL2,
                activebackground=PANEL, activeforeground=ACCENT,
                font=("Segoe UI Semibold", 11),
                indicatoron=True, cursor="hand2",
                disabledforeground="#4b5568",
            )
            rb.pack(side="left", padx=(4, 16))
            self._radio[key] = rb

        row2 = tk.Frame(actions, bg=PANEL)
        row2.pack(fill="x", padx=10, pady=(0, 10))
        self._btns = []
        for label, cmd in (
            ("Restart AnyDesk", lambda: self._run_action("anydesk")),
            ("Refresh", self._refresh),
        ):
            b = tk.Button(
                row2, text=label, command=cmd,
                bg=PANEL2, fg=TEXT, activebackground=ACCENT_DIM,
                activeforeground=TEXT, relief="flat", bd=0,
                font=("Segoe UI Semibold", 10), padx=12, pady=8,
                cursor="hand2",
            )
            b.pack(side="left", padx=4)
            self._btns.append(b)

        self._action_status = tk.Label(
            self.root, text="Space×3 → bring window to front",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
        )
        self._action_status.pack(pady=(0, 12))

    def _minimize(self) -> None:
        self.root.iconify()

    def _bring_front(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _set_error(self, message: str | None) -> None:
        if message:
            self._error_mode = True
            self._err_banner.config(text=f"⚠  {message}")
            if not self._err_banner.winfo_ismapped():
                self._err_banner.pack(fill="x", padx=20, pady=(0, 8),
                                      after=self.root.winfo_children()[0])
            self._bring_front()
        else:
            self._error_mode = False
            self._err_banner.pack_forget()

    # ── data → cards ──────────────────────────────────────────────────────
    def _refresh(self) -> None:
        data = status_store.read(self.status_path)
        if not data:
            self.card_link.set("Waiting…", "core status not ready yet", tone="warn")
            return

        host = data.get("host", "?")
        self._host_lbl.config(text=f"{host}  ·  local control panel")

        conn = data.get("connection", {}) or {}
        tele = data.get("telemetry", {}) or {}
        dev = tele.get("devices", {}) or {}
        state = (conn.get("state") or "unknown").lower()
        if state == "connected":
            self.card_link.set("ONLINE", conn.get("detail", ""), tone="ok")
        elif state == "reconnecting":
            self.card_link.set("RECONNECTING", conn.get("detail", ""), tone="warn")
        else:
            self.card_link.set("OFFLINE", conn.get("detail", ""), tone="bad")

        batt = data.get("battery_pct")
        if batt is None:
            batt = tele.get("battery")
        plugged = data.get("plugged")
        if plugged is None:
            plugged = tele.get("plugged")
        charging = data.get("charging")
        if charging is None:
            charging = tele.get("charging")
        if batt is None:
            self.card_power.set("—", "battery unknown", tone="warn")
        elif plugged:
            tone = "ok" if charging else "info"
            label = f"{batt}% AC"
            sub = "Charging" if charging else "Plugged in"
            self.card_power.set(label, sub, pct=float(batt), tone=tone)
        else:
            tone = "bad" if batt < 20 else "warn" if batt < 40 else "accent"
            self.card_power.set(
                f"{batt}% BATTERY", "AC cut off — on battery",
                pct=float(batt), tone=tone,
            )

        net = data.get("net") or tele.get("net") or "?"
        ip = data.get("ip") or tele.get("ip") or ""
        online = tele.get("internet")
        eth = dev.get("eth", "?")
        wifi = dev.get("wifi", "?")
        sms = dev.get("sms", "?")

        # Bot / core switch: jump radios immediately when status.json updates
        switch_at = data.get("net_switch_at")
        if switch_at and switch_at != self._last_net_switch_at:
            self._last_net_switch_at = switch_at
            self._hold_net = None
            remote = (net or "").split(":")[0].lower()
            if remote in ("wifi", "eth", "sms") and not self._busy:
                self._net_choice.set(remote)

        if online is False:
            tone = "bad"
            title = f"{net} · NO INTERNET"
        else:
            tone = "ok" if online else "info"
            title = f"{net}"
        self.card_net.set(
            title,
            f"IP {ip or '—'}   ·   eth={eth}   wifi={wifi}   sms={sms}",
            tone=tone,
        )

        self._sync_net_radios(net, eth=eth, wifi=wifi, sms=sms)

        cpu = data.get("cpu") if data.get("cpu") is not None else tele.get("cpu")
        ram = data.get("ram") if data.get("ram") is not None else tele.get("ram")
        temp = data.get("temp") if data.get("temp") is not None else tele.get("temp")
        ad = tele.get("anydesk") or {}
        ad_run = ad.get("running")
        ad_el = ad.get("elevated")
        ad_txt = "AnyDesk admin" if ad_run and ad_el else (
            "AnyDesk (not admin)" if ad_run else "AnyDesk off")
        load = float(cpu or 0)
        tone = "bad" if load > 85 else "warn" if load > 65 else "ok"
        self.card_sys.set(
            f"CPU {cpu if cpu is not None else '—'}%  ·  RAM {ram if ram is not None else '—'}%",
            f"Temp {temp if temp is not None else '—'}°C   ·   {ad_txt}",
            pct=load, tone=tone,
        )

        updated = data.get("updated_at")
        if updated and not self._busy:
            age = int(time.time() - updated)
            self._action_status.config(
                text=f"Updated {age}s ago   ·   Space×3 bring to front")

    def _source_available(self, key: str, status: str) -> bool:
        """Allow selecting a path that is up or standby (saved profile)."""
        s = (status or "").lower().strip()
        return s in ("on", "standby", "ready") or s.startswith("on:")

    def _sync_net_radios(self, active_net: str, *, eth: str, wifi: str,
                         sms: str) -> None:
        """Select current route; grey out paths that aren't available."""
        if self._busy:
            return
        cur = (active_net or "").split(":")[0].lower()

        avail = {
            "wifi": self._source_available("wifi", wifi),
            "eth": self._source_available("eth", eth),
            "sms": self._source_available("sms", sms),
        }
        # Also respect .env config (no SSID → never enable)
        if not (self.cfg.wifi_dsl_ssid or "").strip():
            avail["wifi"] = False
        if not (self.cfg.eth_adapter or "").strip():
            avail["eth"] = False
        if not (self.cfg.wifi_sms_ssid or "").strip():
            avail["sms"] = False

        # Manual pick: keep radio on hold until status.json catches up
        # (telemetry can stay on old path for up to STATUS_INTERVAL).
        # Do NOT treat stale net≠hold as auto-failover — that snapped Wi‑Fi
        # back to SMS every second after a successful switch.
        now = time.time()
        pick = None
        if self._hold_net and now < self._hold_net_until:
            if cur == self._hold_net:
                self._hold_net = None
                pick = cur
            elif avail.get(self._hold_net):
                pick = self._hold_net
            else:
                self._hold_net = None
        if pick is None:
            if cur in ("wifi", "eth", "sms") and avail.get(cur):
                pick = cur
            else:
                for key in ("wifi", "sms", "eth"):
                    if avail.get(key):
                        pick = key
                        break
        if pick:
            self._net_choice.set(pick)

        labels = {
            "wifi": "Wi‑Fi (DSL)",
            "eth": "Ethernet",
            "sms": "SMS hotspot",
        }
        status_map = {"wifi": wifi, "eth": eth, "sms": sms}
        for key, rb in self._radio.items():
            st = status_map[key]
            on = avail[key]
            tag = (
                "up" if str(st).startswith("on")
                else ("saved" if str(st).lower() in ("standby", "ready") else str(st))
            )
            rb.config(
                state=("normal" if on and not self._busy else "disabled"),
                text=f"{labels[key]}  ·  {tag}",
            )

    def _poll(self) -> None:
        data = status_store.read(self.status_path)
        alert = data.get("alert") if data else None
        if alert:
            msg = alert.get("message", "error")
            at = alert.get("at")
            if at != self._last_alert_at:
                self._last_alert_at = at
                self._set_error(msg)
        elif self._error_mode:
            self._last_alert_at = None
            self._set_error(None)

        self._refresh()
        self._pulse = (self._pulse + 1) % 2
        color = OK if self._pulse else ACCENT_DIM
        if data and (data.get("connection") or {}).get("state") == "connected":
            self._live_dot.itemconfig(self._live_oval, fill=color)
            self._live_lbl.config(text="LIVE", fg=OK)
        else:
            self._live_dot.itemconfig(self._live_oval, fill=BAD)
            self._live_lbl.config(text="DOWN", fg=BAD)

        self.root.after(1000, self._poll)

    # ── interactive actions ───────────────────────────────────────────────
    def _set_busy(self, msg: str) -> None:
        self._busy = True
        self._action_status.config(text=msg, fg=ACCENT)
        for b in self._btns:
            b.config(state="disabled")
        for rb in self._radio.values():
            rb.config(state="disabled")

    def _clear_busy(self, msg: str, ok: bool = True) -> None:
        self._busy = False
        self._action_status.config(text=msg, fg=OK if ok else BAD)
        for b in self._btns:
            b.config(state="normal")
        self._refresh()

    def _on_net_radio(self, kind: str) -> None:
        if self._busy:
            return
        self._run_action(kind)

    def _run_action(self, kind: str) -> None:
        if self._busy:
            return
        names = {"wifi": "Wi‑Fi DSL", "eth": "Ethernet", "sms": "SMS hotspot",
                 "anydesk": "AnyDesk"}
        self._set_busy(f"Switching to {names.get(kind, kind)}…")

        def worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                cfg = Config.load()
                if kind in ("wifi", "eth", "sms"):
                    # Soft pre-check in UI; real switch runs elevated in core
                    # (user session cannot change InterfaceMetric → eth snapped
                    # back to wifi).
                    import net_request
                    import netctl
                    blocked = loop.run_until_complete(
                        netctl.ensure_path_up(cfg, kind))
                    if blocked is not None:
                        res = blocked
                        ok = False
                    else:
                        rid = net_request.submit(kind)
                        res = net_request.wait_result(rid, timeout=50.0)
                        ok = bool(res.get("ok"))
                    detail = res.get("error") or res.get("output") or ""
                    ip = res.get("ip") or ""
                    msg = (f"Active network → {names[kind]}: "
                           f"{'OK' if ok else detail or 'failed'}"
                           f"{f' ({ip})' if ok and ip else ''}")
                    if ok:
                        def _stick(k=kind):
                            self._hold_net = k
                            self._hold_net_until = time.time() + 180
                            self._net_choice.set(k)
                        self.root.after(0, _stick)
                elif kind == "anydesk":
                    import anydesk_mgr
                    res = loop.run_until_complete(anydesk_mgr.restart(cfg))
                    ok = bool(res.get("ok"))
                    elev = anydesk_mgr.is_elevated()
                    msg = (f"AnyDesk: "
                           f"{'running as admin' if ok and elev else res}")
                else:
                    ok, msg = False, "unknown action"
                loop.close()
            except Exception as exc:                # noqa: BLE001
                ok, msg = False, str(exc)
            self.root.after(0, lambda: self._clear_busy(msg, ok))

        threading.Thread(target=worker, daemon=True).start()

    # ── hotkeys ───────────────────────────────────────────────────────────
    def _hook_proc(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and wParam == WM_KEYDOWN:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.vkCode == VK_SPACE:
                now = time.time()
                if now - self._last_space > self._space_window:
                    self._space_count = 0
                self._space_count += 1
                self._last_space = now
                if self._space_count >= 3:
                    self._space_count = 0
                    self.root.after(0, self._bring_front)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _hook_thread(self) -> None:
        self._hook_cb = LowLevelKeyboardProc(self._hook_proc)
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_cb,
            kernel32.GetModuleHandleW(None), 0,
        )
        if not hook:
            log.error("keyboard hook failed")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hook)

    def run(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        threading.Thread(target=self._hook_thread, daemon=True).start()
        self._refresh()
        self.root.after(500, self._poll)
        log.info("modern status UI open")
        self.root.mainloop()


def main() -> None:
    DisplayApp().run()


if __name__ == "__main__":
    main()
