"""End-to-end test: the wired loop detects an outage, stabilizes, and recovers once."""

from __future__ import annotations

from pathlib import Path

import httpx

from arr_recovery_watchdog.arr import ArrClient, BackoffPolicy, RecoveryRunner
from arr_recovery_watchdog.config import ArrApp
from arr_recovery_watchdog.health import GluetunHealthClient
from arr_recovery_watchdog.machine import RecoveryStateMachine
from arr_recovery_watchdog.main import run_loop
from arr_recovery_watchdog.state import PersistedState, StateStore

from .conftest import FakeClock

PROWLARR_TESTALL = "http://127.0.0.1:9696/api/v1/indexer/testall"
SONARR_TESTALL = "http://sonarr:8989/api/v3/indexer/testall"
RADARR_TESTALL = "http://radarr:7878/api/v3/indexer/testall"


def test_outage_then_stable_recovery_end_to_end(tmp_path: Path, clock: FakeClock) -> None:
    # Health responses consumed one per poll; once exhausted, request shutdown.
    health_script = [500, 200, 200, 200, 200, 200, 200]
    posts: list[str] = []

    def health_handler(request: httpx.Request) -> httpx.Response:
        if not health_script:
            clock.interrupt()
            return httpx.Response(200)
        return httpx.Response(health_script.pop(0))

    def arr_handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200)

    health = GluetunHealthClient(
        "http://127.0.0.1:9999/", httpx.Client(transport=httpx.MockTransport(health_handler))
    )
    store = StateStore(tmp_path / "state.json")
    machine = RecoveryStateMachine.from_persisted(
        PersistedState(),
        stability_seconds=15.0,
        cooldown_seconds=1800.0,
        healthy_now=True,
        mono_now=clock.monotonic(),
        utc_now=clock.now_utc(),
    )

    arr_http = httpx.Client(transport=httpx.MockTransport(arr_handler))

    def build(name: str, base_url: str, path: str) -> ArrClient:
        return ArrClient(
            ArrApp(name=name, base_url=base_url, api_key="k", testall_path=path),
            arr_http,
            retry_count=0,
            backoff=BackoffPolicy(),
            waiter=clock.wait,
            dry_run=False,
        )

    runner = RecoveryRunner(
        build("Prowlarr", "http://127.0.0.1:9696", "/api/v1/indexer/testall"),
        [
            build("Sonarr", "http://sonarr:8989", "/api/v3/indexer/testall"),
            build("Radarr", "http://radarr:7878", "/api/v3/indexer/testall"),
        ],
        settle_seconds=10.0,
        waiter=clock.wait,
        dry_run=False,
    )

    run_loop(
        machine=machine,
        store=store,
        health=health,
        runner=runner,
        clock=clock,
        poll_interval=5.0,
    )

    # Exactly one recovery, Prowlarr first, then Sonarr and Radarr.
    assert posts == [PROWLARR_TESTALL, SONARR_TESTALL, RADARR_TESTALL]

    # The persisted state reflects the completed recovery.
    saved = store.load()
    assert saved.phase == "monitoring"
    assert saved.outage_observed is False
    assert saved.last_recovery_utc is not None
