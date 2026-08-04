"""Persistent watchdog state with atomic file storage."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("watchdog.state")

SCHEMA_VERSION = 1

VALID_PHASES = frozenset({"monitoring", "outage", "stabilizing", "cooldown_wait", "recovering"})


class CorruptStateError(Exception):
    """Raised internally when the state file cannot be interpreted."""


@dataclass(frozen=True)
class PersistedState:
    """The subset of watchdog state that survives restarts. All timestamps are UTC ISO 8601."""

    schema_version: int = SCHEMA_VERSION
    phase: str = "monitoring"
    outage_observed: bool = False
    pending_apps: tuple[str, ...] = ()
    stabilization_started_utc: str | None = None
    last_recovery_utc: str | None = None
    updated_utc: str | None = None


def conservative_fallback() -> PersistedState:
    """The state assumed when previous state exists but cannot be read.

    Assuming an unresolved outage may cause one unnecessary test-all after
    stable health, but can never silently drop a recovery that was needed.
    """
    return PersistedState(phase="outage", outage_observed=True)


class StateStore:
    """Loads and atomically saves watchdog state as JSON.

    A missing file yields fresh defaults (normal first run). A corrupt file is
    quarantined (renamed aside) and replaced with the conservative fallback —
    an assumed unresolved outage — rather than a clean baseline.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def verify_writable(self) -> bool:
        """Probe the state directory with a real write; used as a startup gate.

        Running without durable state would break the persist-before-acting
        guarantee, so callers should refuse to start when this fails.
        """
        probe = self._path.with_name(self._path.name + ".probe")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("probe\n")
            os.unlink(probe)
            return True
        except OSError as exc:
            logger.error("State directory %s is not writable: %s", self._path.parent, exc)
            return False

    def load(self) -> PersistedState:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("No state file at %s; starting with fresh state", self._path)
            return PersistedState()
        except OSError as exc:
            logger.error(
                "Cannot read state file %s (%s); conservatively assuming an unresolved outage",
                self._path,
                exc,
            )
            return conservative_fallback()
        try:
            return self._parse(raw)
        except CorruptStateError as exc:
            logger.error(
                "State file %s is corrupt (%s); quarantining it and conservatively assuming "
                "an unresolved outage",
                self._path,
                exc,
            )
            self._quarantine()
            return conservative_fallback()

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
        pending = data.get("pending_apps", [])
        if not isinstance(pending, list) or not all(isinstance(item, str) for item in pending):
            raise CorruptStateError("pending_apps is not a list of strings")

        def optional_str(key: str) -> str | None:
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                raise CorruptStateError(f"{key} is not a string")
            return value

        return PersistedState(
            schema_version=SCHEMA_VERSION,
            phase=phase,
            outage_observed=outage_observed,
            pending_apps=tuple(pending),
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

    def save(self, state: PersistedState) -> bool:
        """Atomically persist state: write a temp file, fsync, then rename over the target.

        Returns True on success. Failures are logged and reported to the
        caller; the caller must not treat the state as durable until a save
        has succeeded.
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
            return True
        except OSError as exc:
            logger.error("Failed to persist state to %s: %s", self._path, exc)
            return False

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
