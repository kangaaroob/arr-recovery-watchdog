"""Arr client tests: ordering, isolation, retries, 2xx handling, headers, dry run."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from arr_recovery_watchdog.arr import ArrClient, BackoffPolicy, RecoveryRunner
from arr_recovery_watchdog.config import ArrApp

API_KEY = "test-api-key-12345"
PROWLARR_TESTALL = "http://127.0.0.1:9696/api/v1/indexer/testall"
SONARR_TESTALL = "http://sonarr:8989/api/v3/indexer/testall"
RADARR_TESTALL = "http://radarr:7878/api/v3/indexer/testall"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler,
    *,
    name: str = "Sonarr",
    base_url: str = "http://sonarr:8989",
    path: str = "/api/v3/indexer/testall",
    retry_count: int = 0,
    backoff: BackoffPolicy | None = None,
    waiter: Callable[[float], bool] | None = None,
    dry_run: bool = False,
    health_gate: Callable[[], bool] | None = None,
) -> tuple[ArrClient, list[float]]:
    waits: list[float] = []

    def default_waiter(seconds: float) -> bool:
        waits.append(seconds)
        return False

    app = ArrApp(name=name, base_url=base_url, api_key=API_KEY, testall_path=path)
    client = ArrClient(
        app,
        httpx.Client(transport=httpx.MockTransport(handler)),
        retry_count=retry_count,
        backoff=backoff or BackoffPolicy(),
        waiter=waiter or default_waiter,
        dry_run=dry_run,
        health_gate=health_gate,
    )
    return client, waits


def make_runner(
    handler: Handler,
    *,
    retry_count: int = 0,
    settle_seconds: float = 10.0,
    dry_run: bool = False,
    waiter: Callable[[float], bool] | None = None,
    health_gate: Callable[[], bool] | None = None,
) -> tuple[RecoveryRunner, list[float]]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    waits: list[float] = []

    def default_waiter(seconds: float) -> bool:
        waits.append(seconds)
        return False

    active_waiter = waiter or default_waiter

    def build(name: str, base_url: str, path: str) -> ArrClient:
        app = ArrApp(name=name, base_url=base_url, api_key=API_KEY, testall_path=path)
        return ArrClient(
            app,
            http,
            retry_count=retry_count,
            backoff=BackoffPolicy(),
            waiter=active_waiter,
            dry_run=dry_run,
            health_gate=health_gate,
        )

    prowlarr = build("Prowlarr", "http://127.0.0.1:9696", "/api/v1/indexer/testall")
    sonarr = build("Sonarr", "http://sonarr:8989", "/api/v3/indexer/testall")
    radarr = build("Radarr", "http://radarr:7878", "/api/v3/indexer/testall")
    runner = RecoveryRunner(
        prowlarr,
        [sonarr, radarr],
        settle_seconds=settle_seconds,
        waiter=active_waiter,
        dry_run=dry_run,
        health_gate=health_gate,
    )
    return runner, waits


def test_prowlarr_is_called_before_sonarr_and_radarr() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    runner, waits = make_runner(handler)
    outcome = runner.run()
    assert requests[0] == PROWLARR_TESTALL
    assert set(requests[1:]) == {SONARR_TESTALL, RADARR_TESTALL}
    assert 10.0 in waits  # the settle period between Prowlarr and the others
    assert outcome.results == {"Prowlarr": True, "Sonarr": True, "Radarr": True}
    assert outcome.gluetun_unhealthy is False
    assert outcome.interrupted is False


def test_one_failing_app_does_not_block_the_others() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.port == 9696:
            return httpx.Response(500)
        return httpx.Response(200)

    runner, _ = make_runner(handler, retry_count=1)
    outcome = runner.run()
    assert outcome.results == {"Prowlarr": False, "Sonarr": True, "Radarr": True}
    assert SONARR_TESTALL in requests
    assert RADARR_TESTALL in requests


def test_failed_requests_are_retried_with_exponential_backoff() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500 if len(attempts) < 3 else 200)

    client, waits = make_client(handler, retry_count=5)
    assert client.test_all() is True
    assert len(attempts) == 3
    assert waits == [2.0, 4.0]


def test_backoff_is_bounded_by_the_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client, waits = make_client(handler, retry_count=6)
    assert client.test_all() is False
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]  # capped at 30 s
    assert max(waits) <= 30.0


def test_connection_errors_are_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    client, _ = make_client(handler, retry_count=1)
    assert client.test_all() is True
    assert len(calls) == 2


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_any_2xx_response_is_accepted(status: int) -> None:
    client, _ = make_client(lambda request: httpx.Response(status))
    assert client.test_all() is True


@pytest.mark.parametrize("status", [301, 400, 401, 404, 500, 503])
def test_non_2xx_responses_are_failures(status: int) -> None:
    client, _ = make_client(lambda request: httpx.Response(status))
    assert client.test_all() is False


def test_api_key_is_sent_via_x_api_key_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["api_key"] = request.headers.get("X-Api-Key", "")
        captured["method"] = request.method
        return httpx.Response(200)

    client, _ = make_client(handler)
    client.test_all()
    assert captured["api_key"] == API_KEY
    assert captured["method"] == "POST"


def test_dry_run_sends_no_requests(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP request sent in dry-run mode: {request.url}")

    runner, _ = make_runner(handler, dry_run=True)
    import logging

    with caplog.at_level(logging.INFO):
        outcome = runner.run()
    assert outcome.results == {"Prowlarr": True, "Sonarr": True, "Radarr": True}
    assert "DRY RUN" in caplog.text


def test_shutdown_aborts_pending_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    def interrupted_waiter(seconds: float) -> bool:
        return True  # shutdown already requested

    client, _ = make_client(handler, retry_count=5, waiter=interrupted_waiter)
    assert client.test_all() is False
    assert len(calls) == 1  # no further attempts after the shutdown signal


def test_shutdown_during_settle_wait_skips_followers() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    def interrupted_waiter(seconds: float) -> bool:
        return True

    runner, _ = make_runner(handler, waiter=interrupted_waiter)
    outcome = runner.run()
    assert requests == [PROWLARR_TESTALL]
    assert outcome.results == {"Prowlarr": True, "Sonarr": False, "Radarr": False}
    assert outcome.interrupted is True
    assert outcome.gluetun_unhealthy is False


def test_only_restricts_the_run_and_skips_the_settle_wait() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    runner, waits = make_runner(handler)
    outcome = runner.run(only={"Sonarr"})
    assert requests == [SONARR_TESTALL]  # Prowlarr already succeeded; not re-tested
    assert outcome.results == {"Sonarr": True}
    assert waits == []  # no settle wait when Prowlarr is not part of the run


def test_only_prowlarr_runs_without_followers_or_settle() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    runner, waits = make_runner(handler)
    outcome = runner.run(only={"Prowlarr"})
    assert requests == [PROWLARR_TESTALL]
    assert outcome.results == {"Prowlarr": True}
    assert waits == []


def test_unhealthy_gluetun_before_start_aborts_the_whole_sequence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should be sent while Gluetun is unhealthy: {request.url}")

    runner, _ = make_runner(handler, health_gate=lambda: False)
    outcome = runner.run()
    assert outcome.results == {"Prowlarr": False, "Sonarr": False, "Radarr": False}
    assert outcome.gluetun_unhealthy is True


def test_unhealthy_gluetun_after_settle_skips_followers() -> None:
    requests: list[str] = []
    gate_results = iter([True, False])  # healthy before Prowlarr, unhealthy after the settle

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    runner, _ = make_runner(handler, health_gate=lambda: next(gate_results))
    outcome = runner.run()
    assert requests == [PROWLARR_TESTALL]
    assert outcome.results == {"Prowlarr": True, "Sonarr": False, "Radarr": False}
    assert outcome.gluetun_unhealthy is True


def test_gluetun_outage_first_seen_after_the_last_app_is_reported() -> None:
    # Sonarr and Radarr fail at the API level because the VPN is dropping at
    # that very moment; no gate check between apps sees it, but the final
    # re-check does, so the failures are attributed to a new outage.
    gate_results = iter([True, True, True, False])  # before each app, then the final re-check

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.port == 9696 else 500)

    runner, _ = make_runner(handler, health_gate=lambda: next(gate_results))
    outcome = runner.run()
    assert outcome.results == {"Prowlarr": True, "Sonarr": False, "Radarr": False}
    assert outcome.gluetun_unhealthy is True


def test_unhealthy_gluetun_during_backoff_abandons_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    client, _ = make_client(handler, retry_count=5, health_gate=lambda: False)
    assert client.test_all() is False
    assert len(calls) == 1  # no further attempts once Gluetun is unhealthy again
