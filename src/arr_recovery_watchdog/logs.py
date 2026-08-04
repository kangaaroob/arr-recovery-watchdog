"""Logging setup with UTC timestamps and defensive secret redaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable


class SecretRedactingFilter(logging.Filter):
    """Replaces any occurrence of a known secret in log messages with a placeholder.

    Defense in depth: the application never logs secrets deliberately, but this
    filter also scrubs secrets that leak through exception text or URLs.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once; subsequent calls only adjust the level."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        root.addHandler(handler)


def install_redaction(secrets: Iterable[str]) -> None:
    """Attach a secret-redacting filter to every root logging handler."""
    redaction = SecretRedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction)
