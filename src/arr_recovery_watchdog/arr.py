"""Clients for the Arr applications' Test All Indexers endpoints."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field

import httpx

from .config import ArrApp

logger = logging.getLogger("watchdog.arr")

# A waiter sleeps for the given seconds and returns True if shutdown was requested.
Waiter = Callable[[float], bool]

# A health gate returns True while Gluetun is healthy. The recovery sequence
# re-checks it between steps and aborts early on failure, so the main loop can
# return to outage handling instead of hammering APIs during a fresh outage.
HealthGate = Callable[[], bool]


@dataclass(frozen=True)
class RecoveryOutcome:
    """The outcome of one recovery sequence run.

    `gluetun_unhealthy` means the sequence observed Gluetun unhealthy and
    aborted: the caller must report a new outage to the state machine rather
    than record the failures as ordinary partial Arr failures. `interrupted`
    means shutdown cut the sequence short.
    """

    results: Mapping[str, bool] = field(default_factory=dict)
    gluetun_unhealthy: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class BackoffPolicy:
    """Bounded exponential backoff: base * 2^n, capped."""

    base_seconds: float = 2.0
    cap_seconds: float = 30.0

    def delay(self, retry_index: int) -> float:
        return min(self.cap_seconds, self.base_seconds * (2.0**retry_index))


class ArrClient:
    """Calls one Arr application's test-all endpoint with retries and backoff."""

    def __init__(
        self,
        app: ArrApp,
        http: httpx.Client,
        *,
        retry_count: int,
        backoff: BackoffPolicy,
        waiter: Waiter,
        dry_run: bool,
        health_gate: HealthGate | None = None,
    ) -> None:
        self.name = app.name
        self._url = app.testall_url
        self._api_key = app.api_key
        self._http = http
        self._retry_count = retry_count
        self._backoff = backoff
        self._waiter = waiter
        self._dry_run = dry_run
        self._health_gate = health_gate

    def test_all(self) -> bool:
        """POST the test-all endpoint. Any 2xx response is success.

        Retries up to `retry_count` additional times with bounded exponential
        backoff. Returns False if all attempts fail, shutdown interrupts, or
        Gluetun turns unhealthy again during a backoff wait.
        """
        if self._dry_run:
            logger.info("[DRY RUN] %s: would POST %s", self.name, self._url)
            return True

        attempts = self._retry_count + 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                delay = self._backoff.delay(attempt - 2)
                logger.info(
                    "%s: retrying test-all in %.0fs (attempt %d of %d)",
                    self.name,
                    delay,
                    attempt,
                    attempts,
                )
                if self._waiter(delay):
                    logger.warning("%s: shutdown requested; abandoning retries", self.name)
                    return False
                if self._health_gate is not None and not self._health_gate():
                    logger.warning(
                        "%s: Gluetun became unhealthy during backoff; abandoning retries",
                        self.name,
                    )
                    return False
            try:
                response = self._http.post(self._url, headers={"X-Api-Key": self._api_key})
            except httpx.HTTPError as exc:
                logger.warning(
                    "%s: test-all request failed (%s: %s)", self.name, type(exc).__name__, exc
                )
                continue
            if 200 <= response.status_code < 300:
                logger.info("%s: test-all accepted (HTTP %d)", self.name, response.status_code)
                return True
            logger.warning("%s: test-all returned HTTP %d", self.name, response.status_code)
        logger.error("%s: test-all failed after %d attempts", self.name, attempts)
        return False


class RecoveryRunner:
    """Executes the recovery sequence: Prowlarr first, then the other apps.

    A failure in any one application never prevents attempts against the
    others, but if Gluetun itself turns unhealthy mid-sequence the remaining
    applications are abandoned and the outcome flags a new Gluetun outage so
    the state machine can require a fresh stabilization window.
    """

    def __init__(
        self,
        prowlarr: ArrClient,
        followers: list[ArrClient],
        *,
        settle_seconds: float,
        waiter: Waiter,
        dry_run: bool,
        health_gate: HealthGate | None = None,
    ) -> None:
        self._prowlarr = prowlarr
        self._followers = followers
        self._settle_seconds = settle_seconds
        self._waiter = waiter
        self._dry_run = dry_run
        self._health_gate = health_gate

    def run(self, only: Collection[str] | None = None) -> RecoveryOutcome:
        """Run the sequence for the selected applications; None selects all.

        `only` restricts the run to the named applications, used when retrying
        a partially failed recovery so already-successful apps are not
        re-tested. The settle wait only happens when Prowlarr itself was
        tested and followers remain.

        The outcome flags whether Gluetun was observed unhealthy during the
        sequence, so the caller can record a new outage instead of treating
        the aborted applications as ordinary partial failures.
        """
        suffix = " (dry run)" if self._dry_run else ""

        def selected(client: ArrClient) -> bool:
            return only is None or client.name in only

        prowlarr = self._prowlarr if selected(self._prowlarr) else None
        followers = [client for client in self._followers if selected(client)]
        planned: list[ArrClient] = ([prowlarr] if prowlarr is not None else []) + followers
        if not planned:
            logger.warning("Recovery requested but no configured application was selected")
            return RecoveryOutcome()
        logger.info(
            "Starting Arr recovery sequence%s: %s",
            suffix,
            ", ".join(client.name for client in planned),
        )

        results: dict[str, bool] = {}
        if prowlarr is not None:
            if not self._gluetun_healthy():
                return self._abort(results, planned)
            results[prowlarr.name] = self._safe_test_all(prowlarr)

        if followers:
            names = ", ".join(client.name for client in followers)
            if prowlarr is not None and self._settle_seconds > 0:
                logger.info(
                    "Waiting %.0fs for Prowlarr to settle before testing %s",
                    self._settle_seconds,
                    names,
                )
                if self._waiter(self._settle_seconds):
                    logger.warning("Shutdown requested during settle wait; skipping %s", names)
                    for client in followers:
                        results[client.name] = False
                    return RecoveryOutcome(results=results, interrupted=True)
            for client in followers:
                if not self._gluetun_healthy():
                    return self._abort(results, planned)
                results[client.name] = self._safe_test_all(client)

        summary = " ".join(f"{name}={'ok' if ok else 'FAILED'}" for name, ok in results.items())
        logger.info("Arr recovery sequence complete%s: %s", suffix, summary)
        # A failure on the last application (or during its retry backoff) can
        # be the first sign of a new Gluetun outage that no later gate check
        # would see. Re-check once so those failures are attributed correctly.
        if not all(results.values()) and not self._gluetun_healthy():
            logger.warning(
                "Gluetun is unhealthy after the recovery sequence; "
                "treating the failed applications as part of a new outage"
            )
            return RecoveryOutcome(results=results, gluetun_unhealthy=True)
        return RecoveryOutcome(results=results)

    def _gluetun_healthy(self) -> bool:
        return self._health_gate is None or self._health_gate()

    def _abort(self, results: dict[str, bool], planned: list[ArrClient]) -> RecoveryOutcome:
        remaining = [client.name for client in planned if client.name not in results]
        logger.warning(
            "Gluetun is unhealthy; aborting the recovery sequence before testing %s",
            ", ".join(remaining),
        )
        for name in remaining:
            results[name] = False
        return RecoveryOutcome(results=results, gluetun_unhealthy=True)

    @staticmethod
    def _safe_test_all(client: ArrClient) -> bool:
        try:
            return client.test_all()
        except Exception:
            logger.exception("%s: unexpected error during test-all", client.name)
            return False
