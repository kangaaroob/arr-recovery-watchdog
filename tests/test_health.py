"""Gluetun health client tests: every failure mode counts as unhealthy."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from arr_recovery_watchdog.health import GluetunHealthClient


def make_health(handler: Callable[[httpx.Request], httpx.Response]) -> GluetunHealthClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GluetunHealthClient("http://127.0.0.1:9999/", http)


def test_http_200_is_healthy() -> None:
    result = make_health(lambda request: httpx.Response(200)).check()
    assert result.healthy is True


@pytest.mark.parametrize("status", [500, 404, 302, 503, 201])
def test_non_200_statuses_are_unhealthy(status: int) -> None:
    result = make_health(lambda request: httpx.Response(status)).check()
    assert result.healthy is False
    assert str(status) in result.detail


def test_connection_refused_is_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = make_health(handler).check()
    assert result.healthy is False


def test_timeout_is_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = make_health(handler).check()
    assert result.healthy is False
    assert "timeout" in result.detail.lower()


def test_dns_failure_is_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    result = make_health(handler).check()
    assert result.healthy is False


def test_malformed_response_is_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("illegal response", request=request)

    result = make_health(handler).check()
    assert result.healthy is False
