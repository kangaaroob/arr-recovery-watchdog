from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


class FakeClock:
    """Deterministic clock for tests: wait() advances virtual time instead of sleeping."""

    def __init__(self) -> None:
        self._mono = 1000.0
        self._utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.waits: list[float] = []
        self._interrupted = False

    def monotonic(self) -> float:
        return self._mono

    def now_utc(self) -> datetime:
        return self._utc

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._utc += timedelta(seconds=seconds)

    def interrupt(self) -> None:
        """Simulate a shutdown request; subsequent waits return True immediately."""
        self._interrupted = True

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self._interrupted:
            return True
        self.advance(seconds)
        return False


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
