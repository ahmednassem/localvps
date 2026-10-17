"""setup_ui.py — assign physical USB ports once (F2).

Workflow:
  1. Plug fan / SMS into a USB port
  2. Label that **port** as Fan or SMS (saved forever in devices.json)
  3. Later: if something is Present on that port → on (taking power);
     if nothing Present → absent

Unplugging does NOT clear the port role.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

import device_registry as dr
from config import Config

log = logging.getLogger("setup_ui")

ROLE_LABELS = {
    "fan": "Fan port",
    "sms": "SMS port",
    "ignore": "Ignore",
}


class SetupUI:
    def __init__(self, parent: tk.Tk | None = None) -> None:
        self.cfg = Config.load()
        self.reg = dr.load()
        self._known: dict[str, dr.PnpDevice] = {}   # location → device
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._open = True

        if parent:
            self.win = tk.Toplevel(parent)
        else:
            self.win = tk.Tk()
        self.win.title("Local VPS — Device Setup")
        self.win.geometry("960x640")
        self.win.configure(bg="#0d1117")
        self.win.attributes("-topmost", True)

        self._build()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_async(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=60)

    def _build(self) -> None:
        title_f = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        tk.Label(
            self.win, text="USB port setup (one time)",
            font=title_f, fg="#58a6ff", bg="#0d1117",
        ).pack(pady=(12, 4))
        tk.Label(
            self.win,
            text="Label each physical USB port once (Fan / SMS). "
                 "Fan can be power-only (no Windows device) — pick the empty port. "
                 "Status = hub sees power/connection on that port.",
            font=tkfont.Font(family="Segoe UI", size=9),
            fg="#8b949e", bg="#0d1117", wraplength=900,
        ).pack(pady=(0, 8))

        body = tk.Frame(self.win, bg="#0d1117")
        body.pack(fill="both", expand=True, padx=12, pady=8)

        left = tk.LabelFrame(body, text=" Physical USB ports ", bg="#161b22", fg="#c9d1d9")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        cols = ("port", "name", "power", "role")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=14)
        self.tree.heading("port", text="USB port")
        self.tree.heading("name", text="What's plugged")
        self.tree.heading("power", text="Power")
        self.tree.heading("role", text="Role")
        self.tree.column("port", width=160)
        self.tree.column("name", width=240)
        self.tree.column("power", width=70)
        self.tree.column("role", width=120)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<Double-1>", self._on_device_double_click)

        btn_row = tk.Frame(left, bg="#161b22")
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        for role in ("fan", "sms", "ignore"):
            tk.Button(
                btn_row, text=ROLE_LABELS[role],
                command=lambda r=role: self._assign_selected(r),
                bg="#21262d", fg="#c9d1d9", relief="flat", padx=8,
            ).pack(side="left", padx=4)

        right = tk.LabelFrame(body, text=" Internet sources ", bg="#161b22", fg="#c9d1d9")
        right.pack(side="right", fill="both", expand=True, padx=(6, 0))

        tk.Label(right, text="Cable ethernet (not SMS NDIS)", bg="#161b22", fg="#8b949e").pack(
            anchor="w", padx=10, pady=(10, 2))
        self.eth_var = tk.StringVar(value=self.reg.eth_adapter or "Ethernet")
        self.eth_combo = ttk.Combobox(right, textvariable=self.eth_var, width=42)
        self.eth_combo.pack(padx=10, pady=2, fill="x")

        tk.Label(right, text="DSL Wi-Fi profile", bg="#161b22", fg="#8b949e").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.dsl_var = tk.StringVar(value=self.reg.wifi_dsl_ssid or self.cfg.wifi_dsl_ssid)
        self.dsl_combo = ttk.Combobox(right, textvariable=self.dsl_var, width=42)
        self.dsl_combo.pack(padx=10, pady=2, fill="x")

        tk.Label(right, text="SMS hotspot Wi-Fi profile", bg="#161b22", fg="#8b949e").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.sms_wifi_var = tk.StringVar(
            value=self.reg.wifi_sms_ssid or self.cfg.wifi_sms_ssid)
        self.sms_combo = ttk.Combobox(right, textvariable=self.sms_wifi_var, width=42)
        self.sms_combo.pack(padx=10, pady=2, fill="x")

        tk.Button(
            right, text="Save network settings",
            command=self._save_network,
            bg="#238636", fg="white", relief="flat", padx=12, pady=6,
        ).pack(padx=10, pady=16, anchor="w")

        self._status = tk.Label(
            self.win, text="", font=tkfont.Font(family="Segoe UI", size=9),
            fg="#3fb950", bg="#0d1117",
        )
        self._status.pack(pady=8)

    def _on_close(self) -> None:
        self._open = False
        self.win.destroy()

    def _selected_device(self) -> dr.PnpDevice | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._known.get(sel[0])

    def _assign_selected(self, role: str) -> None:
        dev = self._selected_device()
        if not dev:
            messagebox.showinfo("Select port", "Click a USB port in the list first.")
            return
        if role == "ignore":
            if dev.location:
                dr.unassign(self.reg, dev.location)
                self.reg = dr.load()
                self._status.config(text=f"Cleared role on {dev.port}")
                self._refresh_tree()
            return
        self._assign_device(dev, role)

    def _on_device_double_click(self, _evt) -> None:
        dev = self._selected_device()
        if dev:
            self._prompt_new_device(dev)

    def _assign_device(self, dev: dr.PnpDevice, role: str) -> None:
        dr.assign(self.reg, dev, role)
        self.reg = dr.load()
        self._status.config(
            text=f"Saved: {dev.port} → {ROLE_LABELS.get(role, role)} "
                 f"(power follows plug/unplug)")
        self._refresh_tree()

    def _prompt_new_device(self, dev: dr.PnpDevice) -> None:
        if dr.role_for(self.reg, dev.location):
            return
        where = dev.port or "USB port"
        msg = (f"New plug on {where}:\n\n{dev.friendly_name}\n\n"
               f"Assign this PORT (not the device name):")
        popup = tk.Toplevel(self.win)
        popup.title("Assign USB port")
        popup.attributes("-topmost", True)
        popup.geometry("420x220")
        tk.Label(popup, text=msg, wraplength=380, justify="left").pack(pady=16, padx=16)
        row = tk.Frame(popup)
        row.pack(pady=8)
        for role in ("fan", "sms", "ignore"):
            def _pick(r=role, d=dev, w=popup):
                if r == "ignore":
                    w.destroy()
                    return
                self._assign_device(d, r)
                w.destroy()
            tk.Button(row, text=ROLE_LABELS[role], command=_pick, width=14).pack(
                side="left", padx=4)

    def _save_network(self) -> None:
        eth = self.eth_var.get().strip()
        if eth and not dr.is_cable_eth(eth):
            messagebox.showerror(
                "Wrong ethernet",
                "Pick the cable NIC (Realtek / Ethernet), not SMS Remote NDIS.")
            return
        self.reg.eth_adapter = eth
        self.reg.wifi_dsl_ssid = self.dsl_var.get().strip()
        self.reg.wifi_sms_ssid = self.sms_wifi_var.get().strip()
        dr.save(self.reg)
        self._status.config(text="Network settings saved to devices.json")

    def _refresh_tree(self, devices: list[dr.PnpDevice] | None = None) -> None:
        if devices is None:
            devices = list(self._known.values())
        by_loc = {d.location: d for d in devices if d.location}

        # Keep labeled ports visible even when empty
        rows: list[dr.PnpDevice] = []
        seen: set[str] = set()
        for loc, entry in self.reg.assignments.items():
            if loc in by_loc:
                rows.append(by_loc[loc])
            else:
                rows.append(dr.PnpDevice(
                    instance_id=loc,
                    friendly_name="(nothing plugged)",
                    status="absent",
                    present=False,
                    location=loc,
                    port=str(entry.get("port") or dr._parse_port_label(loc)),
                ))
            seen.add(loc)
        for loc, dev in by_loc.items():
            if loc not in seen:
                rows.append(dev)

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._known = {}
        for dev in sorted(rows, key=lambda d: (d.port or "", d.friendly_name.lower())):
            if not dev.location:
                continue
            self._known[dev.location] = dev
            role = dr.role_for(self.reg, dev.location) or "—"
            label = ROLE_LABELS.get(role, role) if role != "—" else "—"
            power = "on" if dev.present else "absent"
            self.tree.insert(
                "", "end", iid=dev.location,
                values=(dev.port or "?", dev.friendly_name, power, label),
            )

    def _refresh_network_combos(self) -> None:
        try:
            adapters = self._run_async(dr.scan_net_adapters())
            names = [a.get("Name", "") for a in adapters if a.get("Name")]
            self.eth_combo["values"] = names
            if names and not self.eth_var.get():
                self.eth_var.set(names[0])
            profiles = self._run_async(dr.scan_wifi_profiles())
            self.dsl_combo["values"] = profiles
            self.sms_combo["values"] = profiles
        except Exception as exc:                    # noqa: BLE001
            log.warning("network scan failed: %s", exc)

    def _tick(self) -> None:
        if not self._open:
            return
        try:
            # Prefer hub port list (includes empty ports — needed for power-only fan)
            try:
                devices = self._run_async(dr.scan_hub_ports())
            except Exception:                       # noqa: BLE001
                devices = self._run_async(dr.scan_pnp_devices())
            added, removed = dr.diff_devices(self._known, devices)
            for loc in removed:
                self._known.pop(loc, None)
            for dev in devices:
                if dev.location:
                    self._known[dev.location] = dev
            for dev in added:
                if dr.role_for(self.reg, dev.location):
                    continue
                # Only auto-prompt when something newly connected (not empty)
                if not dev.present:
                    continue
                self.win.after(100, lambda d=dev: self._prompt_new_device(d))
            self._refresh_tree(devices)
            if not self.eth_combo["values"]:
                self._refresh_network_combos()
        except Exception as exc:                    # noqa: BLE001
            log.warning("device scan tick failed: %s", exc)
        if self._open and self.win.winfo_exists():
            self.win.after(2000, self._tick)

    def run(self) -> None:
        self._refresh_network_combos()
        if isinstance(self.win, tk.Tk):
            self.win.mainloop()


def open_setup(parent: tk.Tk | None = None) -> SetupUI:
    return SetupUI(parent)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    SetupUI().run()


if __name__ == "__main__":
    main()
