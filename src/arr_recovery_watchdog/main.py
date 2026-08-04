"""Service entrypoint: wiring, signal handling, and the main poll loop."""

from __future__ import annotations

import logging
import os
import signal
import threading
import types

import httpx

from . import __version__
from .arr import ArrClient, BackoffPolicy, RecoveryRunner
from .clock import Clock, RealClock
from .config import ArrApp, Config, ConfigError, load_config, redact_url
from .health import GluetunHealthClient
from .logs import install_redaction, setup_logging
from .machine import Action, RecoveryStateMachine
from .state import StateStore

logger = logging.getLogger("watchdog.main")


def _install_signal_handlers(stop: threading.Event) -> None:
    def handle(signum: int, _frame: types.FrameType | None) -> None:
        logger.info("Received %s; shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def run_loop(
    *,
    machine: RecoveryStateMachine,
    store: StateStore,
    health: GluetunHealthClient,
    runner: RecoveryRunner,
    clock: Clock,
    poll_interval: float,
) -> None:
    """Poll Gluetun until shutdown, persisting transitions and running recoveries.

    The wait is interruptible: SIGTERM/SIGINT set the stop event and the loop
    exits promptly. Unexpected per-iteration errors are logged and survived.
    """
    while not clock.wait(poll_interval):
        try:
            result = health.check()
            logger.debug("Health poll: healthy=%s (%s)", result.healthy, result.detail)
            action = machine.observe(result.healthy, clock.monotonic(), clock.now_utc())
            if machine.dirty:
                # Persist before acting: the recovery timestamp must be durable
                # before any API call so failures cannot cause rapid retries.
                store.save(machine.snapshot(clock.now_utc()))
                machine.dirty = False
            if action is Action.RUN_RECOVERY:
                runner.run()
        except Exception:
            logger.exception("Unexpected error in poll loop; continuing")


def _log_startup_summary(config: Config) -> None:
    targets = [config.prowlarr, config.sonarr, config.radarr]
    described = ", ".join(
        f"{app.name}={redact_url(app.base_url)}" for app in targets if app is not None
    )
    logger.info("arr-recovery-watchdog %s starting (dry_run=%s)", __version__, config.dry_run)
    logger.info(
        "Monitoring %s (poll=%.0fs, stability=%.0fs, settle=%.0fs, cooldown=%.0fs); targets: %s",
        redact_url(config.gluetun_health_url),
        config.poll_interval_seconds,
        config.healthy_stability_seconds,
        config.prowlarr_settle_seconds,
        config.action_cooldown_seconds,
        described,
    )


def main() -> int:
    setup_logging("INFO")
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    logging.getLogger().setLevel(config.log_level)
    install_redaction(config.secrets)
    _log_startup_summary(config)

    stop = threading.Event()
    _install_signal_handlers(stop)
    clock = RealClock(stop)

    store = StateStore(config.state_file)
    persisted = store.load()

    health_http = httpx.Client(timeout=httpx.Timeout(config.http_connect_timeout_seconds))
    arr_http = httpx.Client(
        timeout=httpx.Timeout(
            connect=config.http_connect_timeout_seconds,
            read=config.http_response_timeout_seconds,
            write=config.http_response_timeout_seconds,
            pool=config.http_connect_timeout_seconds,
        )
    )
    try:
        health = GluetunHealthClient(config.gluetun_health_url, health_http)
        initial = health.check()
        logger.info(
            "Initial Gluetun health: %s (%s)",
            "healthy" if initial.healthy else "unhealthy",
            initial.detail,
        )
        machine = RecoveryStateMachine.from_persisted(
            persisted,
            stability_seconds=config.healthy_stability_seconds,
            cooldown_seconds=config.action_cooldown_seconds,
            healthy_now=initial.healthy,
            mono_now=clock.monotonic(),
            utc_now=clock.now_utc(),
        )
        if machine.dirty:
            store.save(machine.snapshot(clock.now_utc()))
            machine.dirty = False

        backoff = BackoffPolicy()

        def make_client(app: ArrApp) -> ArrClient:
            return ArrClient(
                app,
                arr_http,
                retry_count=config.api_retry_count,
                backoff=backoff,
                waiter=clock.wait,
                dry_run=config.dry_run,
            )

        followers = [make_client(app) for app in (config.sonarr, config.radarr) if app is not None]
        runner = RecoveryRunner(
            make_client(config.prowlarr),
            followers,
            settle_seconds=config.prowlarr_settle_seconds,
            waiter=clock.wait,
            dry_run=config.dry_run,
        )

        run_loop(
            machine=machine,
            store=store,
            health=health,
            runner=runner,
            clock=clock,
            poll_interval=config.poll_interval_seconds,
        )
    finally:
        health_http.close()
        arr_http.close()
    logger.info("Shutdown complete")
    return 0
