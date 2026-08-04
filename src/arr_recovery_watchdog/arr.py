"""Clients for the Arr applications' Test All Indexers endpoints."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .config import ArrApp

logger = logging.getLogger("watchdog.arr")

# A waiter sleeps for the given seconds and returns True if shutdown was requested.
Waiter = Callable[[float], bool]


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
    ) -> None:
        self.name = app.name
        self._url = app.testall_url
        self._api_key = app.api_key
        self._http = http
        self._retry_count = retry_count
        self._backoff = backoff
        self._waiter = waiter
        self._dry_run = dry_run

    def test_all(self) -> bool:
        """POST the test-all endpoint. Any 2xx response is success.

        Retries up to `retry_count` additional times with bounded exponential
        backoff. Returns False if all attempts fail or shutdown interrupts.
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

    A failure in any one application never prevents attempts against the others.
    """

    def __init__(
        self,
        prowlarr: ArrClient,
        followers: list[ArrClient],
        *,
        settle_seconds: float,
        waiter: Waiter,
        dry_run: bool,
    ) -> None:
        self._prowlarr = prowlarr
        self._followers = followers
        self._settle_seconds = settle_seconds
        self._waiter = waiter
        self._dry_run = dry_run

    def run(self) -> dict[str, bool]:
        suffix = " (dry run)" if self._dry_run else ""
        logger.info("Starting Arr recovery sequence%s", suffix)
        results: dict[str, bool] = {}
        results[self._prowlarr.name] = self._safe_test_all(self._prowlarr)

        if self._followers:
            names = ", ".join(client.name for client in self._followers)
            if self._settle_seconds > 0:
                logger.info(
                    "Waiting %.0fs for Prowlarr to settle before testing %s",
                    self._settle_seconds,
                    names,
                )
                if self._waiter(self._settle_seconds):
                    logger.warning("Shutdown requested during settle wait; skipping %s", names)
                    for client in self._followers:
                        results[client.name] = False
                    return results
            for client in self._followers:
                results[client.name] = self._safe_test_all(client)

        summary = " ".join(f"{name}={'ok' if ok else 'FAILED'}" for name, ok in results.items())
        logger.info("Arr recovery sequence complete%s: %s", suffix, summary)
        return results

    @staticmethod
    def _safe_test_all(client: ArrClient) -> bool:
        try:
            return client.test_all()
        except Exception:
            logger.exception("%s: unexpected error during test-all", client.name)
            return False
