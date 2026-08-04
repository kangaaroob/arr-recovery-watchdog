"""Time and interruptible-sleep abstraction, injectable for deterministic tests."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Source of monotonic time, UTC wall time, and interruptible sleeps."""

    def monotonic(self) -> float: ...

    def now_utc(self) -> datetime: ...

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if shutdown was requested."""
        ...


class RealClock:
    """Clock backed by the system, with waits interruptible by a shutdown event."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event

    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def wait(self, seconds: float) -> bool:
        return self._stop.wait(max(0.0, seconds))
