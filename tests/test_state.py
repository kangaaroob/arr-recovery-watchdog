"""Persistence tests: atomic writes, missing files, corruption handling."""

from __future__ import annotations

import json
import logging

from arr_recovery_watchdog.state import PersistedState, StateStore


def test_missing_file_returns_fresh_defaults(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.load() == PersistedState()


def test_save_roundtrips_and_leaves_no_temp_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = PersistedState(
        phase="cooldown_wait",
        outage_observed=True,
        stabilization_started_utc="2026-01-01T12:00:00+00:00",
        last_recovery_utc="2026-01-01T11:00:00+00:00",
        updated_utc="2026-01-01T12:05:00+00:00",
    )
    store.save(state)
    assert store.load() == state
    assert not (tmp_path / "state.json.tmp").exists()
    # The file on disk is plain JSON with the schema version recorded.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1


def test_save_creates_missing_parent_directory(tmp_path) -> None:
    store = StateStore(tmp_path / "nested" / "dir" / "state.json")
    store.save(PersistedState())
    assert store.load() == PersistedState()


def test_corrupt_json_is_quarantined_and_defaults_returned(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    path.write_text("{this is not json", encoding="utf-8")
    store = StateStore(path)
    with caplog.at_level(logging.ERROR):
        state = store.load()
    assert state == PersistedState()
    assert "corrupt" in caplog.text.lower()
    assert (tmp_path / "state.json.corrupt").exists()
    assert not path.exists()


def test_unknown_schema_version_is_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 99, "phase": "monitoring"}), encoding="utf-8")
    store = StateStore(path)
    with caplog.at_level(logging.ERROR):
        state = store.load()
    assert state == PersistedState()
    assert (tmp_path / "state.json.corrupt").exists()


def test_invalid_field_types_are_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "phase": "monitoring", "outage_observed": "yes"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = StateStore(path).load()
    assert state == PersistedState()


def test_unknown_phase_is_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "phase": "exploded", "outage_observed": False}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = StateStore(path).load()
    assert state == PersistedState()


def test_save_failure_is_logged_not_raised(tmp_path, caplog) -> None:
    target_dir = tmp_path / "readonly"
    target_dir.mkdir()
    store = StateStore(target_dir / "state.json")
    target_dir.chmod(0o500)
    try:
        with caplog.at_level(logging.ERROR):
            store.save(PersistedState())
    finally:
        target_dir.chmod(0o700)
    assert "Failed to persist" in caplog.text
