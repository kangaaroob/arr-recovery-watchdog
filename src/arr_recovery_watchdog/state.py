"""Persistent watchdog state with atomic file storage."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("watchdog.state")

SCHEMA_VERSION = 1

VALID_PHASES = frozenset({"monitoring", "outage", "stabilizing", "cooldown_wait"})


class CorruptStateError(Exception):
    """Raised internally when the state file cannot be interpreted."""


@dataclass(frozen=True)
class PersistedState:
    """The subset of watchdog state that survives restarts. All timestamps are UTC ISO 8601."""

    schema_version: int = SCHEMA_VERSION
    phase: str = "monitoring"
    outage_observed: bool = False
    stabilization_started_utc: str | None = None
    last_recovery_utc: str | None = None
    updated_utc: str | None = None


class StateStore:
    """Loads and atomically saves watchdog state as JSON.

    A missing file yields fresh defaults. A corrupt file is quarantined (renamed
    aside) and replaced with fresh, conservative defaults rather than crashing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> PersistedState:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("No state file at %s; starting with fresh state", self._path)
            return PersistedState()
        except OSError as exc:
            logger.error(
                "Cannot read state file %s (%s); continuing with fresh state", self._path, exc
            )
            return PersistedState()
        try:
            return self._parse(raw)
        except CorruptStateError as exc:
            logger.error(
                "State file %s is corrupt (%s); quarantining it and starting with fresh, "
                "conservative state",
                self._path,
                exc,
            )
            self._quarantine()
            return PersistedState()

    def _parse(self, raw: str) -> PersistedState:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptStateError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CorruptStateError("top-level JSON value is not an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise CorruptStateError(f"unsupported schema_version {version!r}")
        phase = data.get("phase")
        if phase not in VALID_PHASES:
            raise CorruptStateError(f"unknown phase {phase!r}")
        outage_observed = data.get("outage_observed")
        if not isinstance(outage_observed, bool):
            raise CorruptStateError("outage_observed is not a boolean")

        def optional_str(key: str) -> str | None:
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                raise CorruptStateError(f"{key} is not a string")
            return value

        return PersistedState(
            schema_version=SCHEMA_VERSION,
            phase=phase,
            outage_observed=outage_observed,
            stabilization_started_utc=optional_str("stabilization_started_utc"),
            last_recovery_utc=optional_str("last_recovery_utc"),
            updated_utc=optional_str("updated_utc"),
        )

    def _quarantine(self) -> None:
        backup = self._path.with_name(self._path.name + ".corrupt")
        try:
            os.replace(self._path, backup)
            logger.error("Corrupt state file moved to %s for inspection", backup)
        except OSError as exc:
            logger.error("Could not quarantine corrupt state file: %s", exc)

    def save(self, state: PersistedState) -> None:
        """Atomically persist state: write a temp file, fsync, then rename over the target.

        Persistence failures are logged but never crash the watchdog; it keeps
        running with in-memory state.
        """
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(asdict(state), indent=2) + "\n"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
            self._fsync_parent_dir()
            logger.debug("State persisted: phase=%s", state.phase)
        except OSError as exc:
            logger.error(
                "Failed to persist state to %s (%s); continuing with in-memory state",
                self._path,
                exc,
            )

    def _fsync_parent_dir(self) -> None:
        try:
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
