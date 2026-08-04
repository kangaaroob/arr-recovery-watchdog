"""The recovery state machine.

Phases:

    MONITORING     Gluetun healthy, no outage pending. The steady state.
    OUTAGE         An unhealthy poll was observed; waiting for health to return.
    STABILIZING    Healthy after an outage; requiring continuous health for the
                   stabilization window. Any failed poll returns to OUTAGE.
    COOLDOWN_WAIT  Stability achieved but the recovery cooldown is still active;
                   the recovery stays pending until the cooldown expires, as
                   long as Gluetun remains healthy.

Stabilization is measured with monotonic time (immune to wall-clock jumps).
The recovery cooldown is measured with UTC wall time so it survives restarts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum

from .state import SCHEMA_VERSION, PersistedState

logger = logging.getLogger("watchdog.machine")


class Phase(Enum):
    MONITORING = "monitoring"
    OUTAGE = "outage"
    STABILIZING = "stabilizing"
    COOLDOWN_WAIT = "cooldown_wait"


class Action(Enum):
    RUN_RECOVERY = "run_recovery"


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Ignoring unparseable persisted timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds")


class RecoveryStateMachine:
    """Pure state machine: consumes health observations, emits recovery actions.

    All I/O (HTTP, persistence, sleeping) lives outside; this class only decides.
    The `dirty` flag tells the caller when a meaningful transition occurred and
    the state should be persisted — steady healthy polling never sets it.
    """

    def __init__(self, *, stability_seconds: float, cooldown_seconds: float) -> None:
        self.stability_seconds = stability_seconds
        self.cooldown_seconds = cooldown_seconds
        self.phase = Phase.MONITORING
        self.outage_observed = False
        self.last_recovery_utc: datetime | None = None
        self._stabilization_start_mono: float | None = None
        self._stabilization_start_utc: datetime | None = None
        self.dirty = False

    @classmethod
    def from_persisted(
        cls,
        persisted: PersistedState,
        *,
        stability_seconds: float,
        cooldown_seconds: float,
        healthy_now: bool,
        mono_now: float,
        utc_now: datetime,
    ) -> RecoveryStateMachine:
        """Reconstruct the machine at startup from persisted state plus a fresh health check.

        Conservative restart semantics: a stabilization window in progress when
        the process stopped is restarted from scratch, because health cannot be
        proven continuous while the watchdog was down. The recovery cooldown is
        wall-clock based and carries across restarts unchanged.
        """
        machine = cls(stability_seconds=stability_seconds, cooldown_seconds=cooldown_seconds)
        machine.last_recovery_utc = _parse_utc(persisted.last_recovery_utc)

        if healthy_now:
            if persisted.outage_observed:
                machine.outage_observed = True
                machine._enter_stabilizing(
                    mono_now,
                    utc_now,
                    reason=(
                        f"Startup: Gluetun healthy but an unresolved outage was recorded "
                        f"before restart (previous phase: {persisted.phase}); "
                        f"restarting stabilization window"
                    ),
                )
            else:
                machine.phase = Phase.MONITORING
                logger.info("Startup: Gluetun healthy; baseline established, no action needed")
        else:
            machine.phase = Phase.OUTAGE
            machine.outage_observed = True
            if persisted.outage_observed:
                logger.info("Startup: Gluetun unhealthy; continuing outage recorded before restart")
            else:
                logger.info("Startup: Gluetun unhealthy; outage recorded")

        machine.dirty = not machine._matches(persisted)
        return machine

    def _matches(self, persisted: PersistedState) -> bool:
        return (
            persisted.phase == self.phase.value
            and persisted.outage_observed == self.outage_observed
            and persisted.stabilization_started_utc == _iso(self._stabilization_start_utc)
            and persisted.last_recovery_utc == _iso(self.last_recovery_utc)
        )

    def snapshot(self, utc_now: datetime) -> PersistedState:
        return PersistedState(
            schema_version=SCHEMA_VERSION,
            phase=self.phase.value,
            outage_observed=self.outage_observed,
            stabilization_started_utc=_iso(self._stabilization_start_utc),
            last_recovery_utc=_iso(self.last_recovery_utc),
            updated_utc=_iso(utc_now),
        )

    def cooldown_remaining(self, utc_now: datetime) -> float:
        if self.last_recovery_utc is None:
            return 0.0
        elapsed = (utc_now - self.last_recovery_utc).total_seconds()
        if elapsed < 0:
            # Wall clock moved backwards; be conservative and hold the full cooldown.
            elapsed = 0.0
        return max(0.0, self.cooldown_seconds - elapsed)

    def observe(self, healthy: bool, mono_now: float, utc_now: datetime) -> Action | None:
        """Feed one health observation into the machine; may emit a recovery action.

        When Action.RUN_RECOVERY is returned, the recovery timestamp has already
        been recorded — the caller should persist the snapshot before executing
        the recovery so failures cannot cause rapid repeated attempts.
        """
        if healthy:
            return self._observe_healthy(mono_now, utc_now)
        self._observe_unhealthy(mono_now)
        return None

    def _observe_unhealthy(self, mono_now: float) -> None:
        if self.phase is Phase.OUTAGE:
            logger.debug("Gluetun still unhealthy")
            return
        if self.phase is Phase.MONITORING:
            logger.info("Outage detected: Gluetun health check failed")
        elif self.phase is Phase.STABILIZING:
            start = self._stabilization_start_mono
            elapsed = 0.0 if start is None else mono_now - start
            logger.info(
                "Stabilization reset: Gluetun unhealthy again after %.0fs of health", elapsed
            )
        elif self.phase is Phase.COOLDOWN_WAIT:
            logger.info(
                "Pending recovery cancelled: Gluetun unhealthy again; returning to outage handling"
            )
        self.phase = Phase.OUTAGE
        self.outage_observed = True
        self._stabilization_start_mono = None
        self._stabilization_start_utc = None
        self.dirty = True

    def _observe_healthy(self, mono_now: float, utc_now: datetime) -> Action | None:
        if self.phase is Phase.MONITORING:
            logger.debug("Gluetun healthy")
            return None

        if self.phase is Phase.OUTAGE:
            self._enter_stabilizing(mono_now, utc_now, reason="Gluetun healthy again after outage")
            self.dirty = True
            return None

        if self.phase is Phase.STABILIZING:
            start = self._stabilization_start_mono
            if start is None:  # defensive; cannot happen through normal transitions
                self._enter_stabilizing(mono_now, utc_now, reason="Stabilization restarted")
                self.dirty = True
                return None
            elapsed = mono_now - start
            if elapsed < self.stability_seconds:
                logger.debug("Stabilizing: %.0fs of %.0fs", elapsed, self.stability_seconds)
                return None
            remaining = self.cooldown_remaining(utc_now)
            if remaining > 0:
                self.phase = Phase.COOLDOWN_WAIT
                self.dirty = True
                logger.info(
                    "Gluetun stable for %.0fs, but the recovery cooldown is active; "
                    "recovery deferred for %.0fs",
                    elapsed,
                    remaining,
                )
                return None
            return self._trigger(utc_now, f"Gluetun stable for {elapsed:.0f}s")

        if self.phase is Phase.COOLDOWN_WAIT:
            remaining = self.cooldown_remaining(utc_now)
            if remaining > 0:
                logger.debug("Recovery pending: cooldown has %.0fs remaining", remaining)
                return None
            return self._trigger(utc_now, "Cooldown expired with Gluetun still healthy")

        return None

    def _enter_stabilizing(self, mono_now: float, utc_now: datetime, reason: str) -> None:
        self.phase = Phase.STABILIZING
        self._stabilization_start_mono = mono_now
        self._stabilization_start_utc = utc_now
        logger.info(
            "%s; requiring %.0fs of continuous health before recovery",
            reason,
            self.stability_seconds,
        )

    def _trigger(self, utc_now: datetime, reason: str) -> Action:
        self.last_recovery_utc = utc_now
        self.phase = Phase.MONITORING
        self.outage_observed = False
        self._stabilization_start_mono = None
        self._stabilization_start_utc = None
        self.dirty = True
        logger.info("%s; initiating Arr recovery sequence", reason)
        return Action.RUN_RECOVERY
