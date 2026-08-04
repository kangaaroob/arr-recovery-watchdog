"""Secret-redaction tests: API keys must never reach log output."""

from __future__ import annotations

import logging

import httpx
import pytest

from arr_recovery_watchdog.logs import SecretRedactingFilter, install_redaction

from .test_arr import API_KEY, make_client

SECRET = "super-secret-token-42"


def test_redaction_filter_scrubs_secrets_from_records() -> None:
    record = logging.LogRecord(
        name="watchdog.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="the key is %s",
        args=(SECRET,),
        exc_info=None,
    )
    redaction = SecretRedactingFilter([SECRET])
    assert redaction.filter(record) is True
    assert SECRET not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_redaction_filter_passes_clean_records_through() -> None:
    record = logging.LogRecord(
        name="watchdog.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="nothing sensitive here",
        args=None,
        exc_info=None,
    )
    redaction = SecretRedactingFilter([SECRET])
    assert redaction.filter(record) is True
    assert record.getMessage() == "nothing sensitive here"


def test_redaction_filter_on_a_stream_handler(capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("watchdog.redaction-e2e")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter([SECRET]))
    logger.addHandler(handler)
    try:
        logger.info("connecting with key %s", SECRET)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    err = capsys.readouterr().err
    assert SECRET not in err
    assert "[REDACTED]" in err


def test_install_redaction_attaches_filter_to_root_handlers() -> None:
    root = logging.getLogger()
    probe = logging.NullHandler()
    root.addHandler(probe)
    try:
        install_redaction([SECRET])
        assert any(isinstance(f, SecretRedactingFilter) for f in probe.filters)
    finally:
        for log_filter in list(probe.filters):
            probe.removeFilter(log_filter)
        root.removeHandler(probe)


def test_arr_client_logs_never_contain_the_api_key(caplog: pytest.LogCaptureFixture) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client, _ = make_client(failing_handler, retry_count=2)
    with caplog.at_level(logging.DEBUG):
        client.test_all()
    assert caplog.text  # something was logged about the failures
    assert API_KEY not in caplog.text


def test_arr_client_exception_logs_never_contain_the_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def exploding_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = make_client(exploding_handler, retry_count=1)
    with caplog.at_level(logging.DEBUG):
        client.test_all()
    assert API_KEY not in caplog.text
