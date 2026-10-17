"""service.py — the native Windows service (the systemctl-like core).

Registers `LocalServerCore` with the Service Control Manager via pywin32. It
starts at boot as LocalSystem (full admin), runs at HIGH priority, and hosts
the single asyncio runtime for:
  - agent (control tunnel + telemetry)
  - kill switch (autonomous overheat protection)
  - network / power / anydesk / rustdesk watchdogs
(No process supervisor — process control moved to the local services side.)

Manage it like any service:
  python service.py install     # register (then set auto-start + restart policy)
  python service.py start | stop | remove
  sc query LocalServerCore
  python service.py foreground  # run in this console for testing (no SCM)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from config import Config

HERE = os.path.dirname(os.path.abspath(__file__))


def _configure_logging(to_file: bool) -> None:
    handlers = [logging.StreamHandler()]
    if to_file:
        cfg_dir = os.environ.get("LOG_DIR") or os.path.join(HERE, "logs")
        os.makedirs(cfg_dir, exist_ok=True)
        handlers = [logging.FileHandler(os.path.join(cfg_dir, "core.log"),
                                        encoding="utf-8")]
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _set_high_priority() -> None:
    try:
        import win32api          # type: ignore
        import win32process      # type: ignore
        win32process.SetPriorityClass(win32api.GetCurrentProcess(),
                                      win32process.HIGH_PRIORITY_CLASS)
    except Exception:                                # noqa: BLE001
        pass


async def run_core(stop_event: asyncio.Event) -> None:
    """The shared runtime used by both the service and foreground mode."""
    from agent import Agent
    from anydesk_watchdog import AnyDeskWatchdog
    from killswitch import KillSwitch
    from net_watchdog import NetWatchdog
    from net_secondary import SecondaryNetWatch
    from power_watch import PowerWatch

    os.chdir(HERE)
    cfg = Config.load()
    _set_high_priority()

    log = logging.getLogger("core")
    import device_registry
    import netdiscover
    await netdiscover.resolve(cfg)
    device_registry.apply_to_config(cfg)
    log.info("core starting (device=%s host=%s, control=%s)",
             cfg.device_id, cfg.host_name, cfg.control_url)

    # Default network: Wi‑Fi DSL. Only restore SMS if user chose that path.
    try:
        from net_startup import ensure_default_network
        await ensure_default_network(cfg, reason="startup", patch_status=True)
    except Exception as exc:                        # noqa: BLE001
        log.warning("default network at start failed: %s", exc)

    try:
        from anydesk_watchdog import ensure_running
        ad = await ensure_running(cfg)
        log.info("anydesk at start: %s",
                 ad.get("output") or ad.get("error") or ad.get("ok"))
    except Exception as exc:                        # noqa: BLE001
        log.warning("anydesk at start failed: %s", exc)

    agent = Agent(cfg)
    killswitch = KillSwitch(cfg, agent.notify)
    net_watch = NetWatchdog(cfg, agent.notify)
    sec_watch = SecondaryNetWatch(cfg, agent.notify)
    power_watch = PowerWatch(cfg, agent.notify)
    ad_watch = AnyDeskWatchdog(cfg)
    from net_request_watch import NetRequestWatch
    from resume_watch import ResumeWatch
    net_req_watch = NetRequestWatch(cfg)
    resume_watch = ResumeWatch(cfg, notify_wake=agent.notify_wake)

    if cfg.dim_backlight_on_start:
        try:
            import backlight
            backlight.dim(cfg, cfg.backlight_dim_level)
            log.info("backlight dimmed to %s%% at core start", cfg.backlight_dim_level)
        except Exception as exc:                    # noqa: BLE001
            log.warning("backlight dim at start failed: %s", exc)

    agent.secondary_net = sec_watch  # type: ignore[attr-defined]

    import peer
    from version import VERSION
    peer.LINK = peer.from_env(
        cfg.device_id, state=lambda: {"vps_link": agent._ws is not None,
                                      "version": VERSION})

    import cluster_local
    import plugs
    import psutil
    from winutil import run as win_run

    def _on_battery():
        b = psutil.sensors_battery()
        return None if b is None else not b.power_plugged

    async def _shutdown():
        code, out = await win_run("shutdown /s /f /t 3", timeout=10)
        return {"ok": code == 0, "output": out}

    cluster_local.LEAD = cluster_local.LeadFallback(
        cfg.device_id, peer.LINK, vps_ok=lambda: agent._ws is not None,
        notify=agent.notify, plug=plugs.run, on_battery=_on_battery,
        shutdown=_shutdown)

    import local_api
    local_api.API = local_api.LocalApi(
        cfg.device_id, peer.LINK,
        self_state=lambda: {"vps_link": agent._ws is not None,
                            "peer_connected": peer.LINK.connected,
                            "lead": cluster_local.LEAD.status()},
        plug=lambda op, n: plugs.run(op, n))

    tasks = [
        asyncio.create_task(local_api.API.run()),
        asyncio.create_task(peer.LINK.run()),
        asyncio.create_task(cluster_local.LEAD.run()),
        asyncio.create_task(agent.run()),
        asyncio.create_task(killswitch.run()),
        asyncio.create_task(net_watch.run()),
        asyncio.create_task(sec_watch.run()),
        asyncio.create_task(power_watch.run()),
        asyncio.create_task(ad_watch.run()),
        asyncio.create_task(net_req_watch.run()),
        asyncio.create_task(resume_watch.run()),
    ]
    try:
        await stop_event.wait()
    finally:
        log.info("core stopping")
        for t in tasks:
            t.cancel()


def _run_foreground() -> None:
    _configure_logging(to_file=False)

    async def _main():
        stop = asyncio.Event()
        await run_core(stop)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


# ── Windows service plumbing (only when pywin32 is present) ──────────────────
try:
    import servicemanager        # type: ignore
    import win32event            # type: ignore
    import win32service          # type: ignore
    import win32serviceutil      # type: ignore
    _HAVE_SVC = True
except Exception:                                    # noqa: BLE001
    _HAVE_SVC = False


if _HAVE_SVC:
    class LocalServerService(win32serviceutil.ServiceFramework):
        _svc_name_ = "LocalServerCore"
        _svc_display_name_ = "Local Server Core"
        _svc_description_ = ("Always-on remote control core: control tunnel, "
                             "telemetry, network control and overheat kill "
                             "switch.")

        def __init__(self, args):
            super().__init__(args)
            self._hstop = win32event.CreateEvent(None, 0, 0, None)
            self._loop = None
            self._async_stop = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._hstop)
            if self._loop is not None and self._async_stop is not None:
                self._loop.call_soon_threadsafe(self._async_stop.set)

        def SvcDoRun(self):
            _configure_logging(to_file=True)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            try:
                asyncio.run(self._amain())
            except Exception:                        # noqa: BLE001
                logging.getLogger("service").exception("core crashed")
                raise

        async def _amain(self):
            self._loop = asyncio.get_running_loop()
            self._async_stop = asyncio.Event()
            await run_core(self._async_stop)


def _main() -> None:
    if len(sys.argv) > 1 and sys.argv[1].lower() == "foreground":
        _run_foreground()
        return
    if not _HAVE_SVC:
        print("pywin32 not installed; only 'foreground' mode is available.")
        sys.exit(1)
    if len(sys.argv) == 1:
        # launched by the SCM
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(LocalServerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(LocalServerService)


if __name__ == "__main__":
    _main()
