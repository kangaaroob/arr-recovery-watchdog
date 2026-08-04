"""End-to-end tests of the wired loop: detection, stabilization, recovery, persistence gating."""

from __future__ import annotations

from pathlib import Path

import httpx

from arr_recovery_watchdog.arr import ArrClient, BackoffPolicy, RecoveryRunner
from arr_recovery_watchdog.config import ArrApp
from arr_recovery_watchdog.health import GluetunHealthClient
from arr_recovery_watchdog.machine import Phase, RecoveryStateMachine
from arr_recovery_watchdog.main import run_loop
from arr_recovery_watchdog.state import PersistedState, StateStore

from .conftest import FakeClock

PROWLARR_TESTALL = "http://127.0.0.1:9696/api/v1/indexer/testall"
SONARR_TESTALL = "http://sonarr:8989/api/v3/indexer/testall"
RADARR_TESTALL = "http://radarr:7878/api/v3/indexer/testall"


class Harness:
    """Wires a full loop against scripted health responses and an Arr handler."""

    def __init__(
        self,
        tmp_path: Path,
        clock: FakeClock,
        health_script: list[int],
        arr_handler,
        store: StateStore | None = None,
        use_health_gate: bool = False,
    ) -> None:
        self.clock = clock
        self.posts: list[str] = []
        self._script = health_script

        def health_handler(request: httpx.Request) -> httpx.Response:
            if not self._script:
                clock.interrupt()
                return httpx.Response(200)
            return httpx.Response(self._script.pop(0))

        self.health = GluetunHealthClient(
            "http://127.0.0.1:9999/", httpx.Client(transport=httpx.MockTransport(health_handler))
        )
        self.store = store or StateStore(tmp_path / "state.json")
        self.machine = RecoveryStateMachine.from_persisted(
            PersistedState(),
            stability_seconds=15.0,
            cooldown_seconds=1800.0,
            app_names=("Prowlarr", "Sonarr", "Radarr"),
            healthy_now=True,
            mono_now=clock.monotonic(),
            utc_now=clock.now_utc(),
        )
        arr_http = httpx.Client(transport=httpx.MockTransport(arr_handler))
        # Same wiring as main(): the gate polls the real health client, so each
        # gate check consumes one entry of the health script.
        gate = (lambda: self.health.check().healthy) if use_health_gate else None

        def build(name: str, base_url: str, path: str) -> ArrClient:
            return ArrClient(
                ArrApp(name=name, base_url=base_url, api_key="k", testall_path=path),
                arr_http,
                retry_count=0,
                backoff=BackoffPolicy(),
                waiter=clock.wait,
                dry_run=False,
                health_gate=gate,
            )

        self.runner = RecoveryRunner(
            build("Prowlarr", "http://127.0.0.1:9696", "/api/v1/indexer/testall"),
            [
                build("Sonarr", "http://sonarr:8989", "/api/v3/indexer/testall"),
                build("Radarr", "http://radarr:7878", "/api/v3/indexer/testall"),
            ],
            settle_seconds=10.0,
            waiter=clock.wait,
            dry_run=False,
            health_gate=gate,
        )

    def run(self) -> None:
        run_loop(
            machine=self.machine,
            store=self.store,
            health=self.health,
            runner=self.runner,
            clock=self.clock,
            poll_interval=5.0,
        )


def test_outage_then_stable_recovery_end_to_end(tmp_path: Path, clock: FakeClock) -> None:
    posts: list[str] = []

    def arr_handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200)

    harness = Harness(tmp_path, clock, [500, 200, 200, 200, 200, 200, 200], arr_handler)
    harness.run()

    # Exactly one recovery, Prowlarr first, then Sonarr and Radarr.
    assert posts == [PROWLARR_TESTALL, SONARR_TESTALL, RADARR_TESTALL]

    # The persisted state reflects the completed recovery.
    saved = harness.store.load()
    assert saved.phase == "monitoring"
    assert saved.outage_observed is False
    assert saved.pending_apps == ()
    assert saved.last_recovery_utc is not None


def test_partially_failed_recovery_persists_the_pending_app(
    tmp_path: Path, clock: FakeClock
) -> None:
    posts: list[str] = []

    def arr_handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        if request.url.host == "sonarr":
            return httpx.Response(500)
        return httpx.Response(200)

    harness = Harness(tmp_path, clock, [500, 200, 200, 200, 200, 200, 200], arr_handler)
    harness.run()

    assert posts == [PROWLARR_TESTALL, SONARR_TESTALL, RADARR_TESTALL]

    # Sonarr failed: the recovery is not considered complete, and a restart
    # would retry only Sonarr after stability and cooldown.
    saved = harness.store.load()
    assert saved.phase == "cooldown_wait"
    assert saved.outage_observed is True
    assert saved.pending_apps == ("Sonarr",)


def test_gluetun_outage_during_recovery_returns_to_outage_not_cooldown(
    tmp_path: Path, clock: FakeClock
) -> None:
    posts: list[str] = []

    def arr_handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200)

    # Script: outage (500), four healthy polls to pass the 15 s stabilization
    # window and trigger the recovery, then — inside the recovery sequence —
    # the gate sees 200 before Prowlarr but 500 before Sonarr (Gluetun went
    # down mid-recovery). The final 200 is the next ordinary poll, with
    # Gluetun healthy again before the poll interval elapsed.
    script = [500, 200, 200, 200, 200, 200, 500, 200]
    harness = Harness(tmp_path, clock, script, arr_handler, use_health_gate=True)
    harness.run()

    # Only Prowlarr was tested; the rest of the sequence was aborted.
    assert posts == [PROWLARR_TESTALL]

    # The mid-recovery gate failure must be recorded as a fresh outage: the
    # healthy poll that follows starts a NEW stabilization window (with the
    # full sequence required again) instead of leaving the machine parked in
    # cooldown_wait as if this were an ordinary partial Arr failure.
    assert harness.machine.phase is Phase.STABILIZING
    saved = harness.store.load()
    assert saved.phase == "stabilizing"
    assert saved.outage_observed is True
    assert saved.pending_apps == ()


class FlakyStore(StateStore):
    """StateStore whose first `fail_count` saves report failure."""

    def __init__(self, path: Path, fail_count: int) -> None:
        super().__init__(path)
        self.fail_count = fail_count

    def save(self, state: PersistedState) -> bool:
        if self.fail_count > 0:
            self.fail_count -= 1
            return False
        return super().save(state)


def test_recovery_is_deferred_while_state_cannot_be_persisted(
    tmp_path: Path, clock: FakeClock
) -> None:
    def arr_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no recovery may run while state is not durable: {request.url}")

    store = FlakyStore(tmp_path / "state.json", fail_count=1000)
    harness = Harness(tmp_path, clock, [500] + [200] * 8, arr_handler, store=store)
    harness.run()

    assert harness.machine.phase is Phase.RECOVERING  # still due, never executed
    assert not (tmp_path / "state.json").exists()


def test_recovery_runs_once_persistence_succeeds_again(tmp_path: Path, clock: FakeClock) -> None:
    posts: list[str] = []

    def arr_handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200)

    # Saves fail through the trigger transition, then start working: the
    # deferred recovery must run on the next healthy poll, not be lost.
    store = FlakyStore(tmp_path / "state.json", fail_count=3)
    harness = Harness(tmp_path, clock, [500] + [200] * 8, arr_handler, store=store)
    harness.run()

    assert posts == [PROWLARR_TESTALL, SONARR_TESTALL, RADARR_TESTALL]
    saved = store.load()
    assert saved.phase == "monitoring"
    assert saved.outage_observed is False
