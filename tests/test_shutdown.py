"""Graceful-shutdown tests: waits and the poll loop must stop promptly."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from arr_recovery_watchdog.arr import ArrClient, BackoffPolicy, RecoveryRunner
from arr_recovery_watchdog.clock import RealClock
from arr_recovery_watchdog.config import ArrApp
from arr_recovery_watchdog.health import GluetunHealthClient
from arr_recovery_watchdog.machine import RecoveryStateMachine
from arr_recovery_watchdog.main import run_loop
from arr_recovery_watchdog.state import PersistedState, StateStore


def test_real_clock_wait_is_interrupted_promptly() -> None:
    stop = threading.Event()
    clock = RealClock(stop)
    timer = threading.Timer(0.05, stop.set)
    timer.start()
    started = time.monotonic()
    interrupted = clock.wait(30.0)
    elapsed = time.monotonic() - started
    assert interrupted is True
    assert elapsed < 5.0  # far below the requested 30 s


def test_real_clock_wait_returns_immediately_when_already_stopped() -> None:
    stop = threading.Event()
    stop.set()
    clock = RealClock(stop)
    started = time.monotonic()
    assert clock.wait(30.0) is True
    assert time.monotonic() - started < 1.0


def test_run_loop_exits_promptly_on_shutdown(tmp_path: Path) -> None:
    stop = threading.Event()
    clock = RealClock(stop)
    poll_count = 0

    def health_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 3:
            stop.set()
        return httpx.Response(200)

    def unreachable_arr_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no recovery should run in this scenario")

    health = GluetunHealthClient(
        "http://127.0.0.1:9999/", httpx.Client(transport=httpx.MockTransport(health_handler))
    )
    machine = RecoveryStateMachine.from_persisted(
        PersistedState(),
        stability_seconds=60.0,
        cooldown_seconds=1800.0,
        app_names=("Prowlarr",),
        healthy_now=True,
        mono_now=clock.monotonic(),
        utc_now=clock.now_utc(),
    )
    arr_http = httpx.Client(transport=httpx.MockTransport(unreachable_arr_handler))
    prowlarr = ArrClient(
        ArrApp("Prowlarr", "http://127.0.0.1:9696", "key", "/api/v1/indexer/testall"),
        arr_http,
        retry_count=0,
        backoff=BackoffPolicy(),
        waiter=clock.wait,
        dry_run=False,
    )
    runner = RecoveryRunner(prowlarr, [], settle_seconds=0.0, waiter=clock.wait, dry_run=False)

    started = time.monotonic()
    run_loop(
        machine=machine,
        store=StateStore(tmp_path / "state.json"),
        health=health,
        runner=runner,
        clock=clock,
        poll_interval=0.01,
    )
    elapsed = time.monotonic() - started
    assert poll_count >= 3
    assert elapsed < 10.0
