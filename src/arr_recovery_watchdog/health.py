"""Client for Gluetun's internal health endpoint."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("watchdog.health")


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    detail: str


class GluetunHealthClient:
    """Polls Gluetun's health endpoint; only a clean HTTP 200 counts as healthy."""

    def __init__(self, url: str, http: httpx.Client) -> None:
        self._url = url
        self._http = http

    def check(self) -> HealthResult:
        try:
            response = self._http.get(self._url)
        except httpx.TimeoutException as exc:
            return HealthResult(False, f"timeout ({type(exc).__name__})")
        except httpx.HTTPError as exc:
            return HealthResult(False, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # any failure means "unhealthy"; the watchdog must never crash
            return HealthResult(False, f"unexpected {type(exc).__name__}: {exc}")
        if response.status_code == 200:
            return HealthResult(True, "HTTP 200")
        return HealthResult(False, f"HTTP {response.status_code}")
