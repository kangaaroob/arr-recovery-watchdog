"""Persistence tests: atomic writes, missing files, corruption handling, write probes."""

from __future__ import annotations

import json
import logging

from arr_recovery_watchdog.state import PersistedState, StateStore, conservative_fallback

CONSERVATIVE = PersistedState(phase="outage", outage_observed=True)


def test_missing_file_returns_fresh_defaults(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.load() == PersistedState()


def test_conservative_fallback_assumes_an_outage() -> None:
    state = conservative_fallback()
    assert state.phase == "outage"
    assert state.outage_observed is True


def test_save_roundtrips_and_leaves_no_temp_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = PersistedState(
        phase="cooldown_wait",
        outage_observed=True,
        pending_apps=("Sonarr", "Radarr"),
        stabilization_started_utc="2026-01-01T12:00:00+00:00",
        last_recovery_utc="2026-01-01T11:00:00+00:00",
        updated_utc="2026-01-01T12:05:00+00:00",
    )
    assert store.save(state) is True
    assert store.load() == state
    assert not (tmp_path / "state.json.tmp").exists()
    # The file on disk is plain JSON with the schema version recorded.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["pending_apps"] == ["Sonarr", "Radarr"]


def test_recovering_phase_roundtrips(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = PersistedState(
        phase="recovering", outage_observed=True, pending_apps=("Prowlarr",)
    )
    assert store.save(state) is True
    assert store.load() == state


def test_state_file_without_pending_apps_field_still_loads(tmp_path) -> None:
    """State files written before pending_apps existed must remain readable."""
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "phase": "outage", "outage_observed": True}
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = StateStore(path).load()
    assert state.phase == "outage"
    assert state.pending_apps == ()


def test_save_creates_missing_parent_directory(tmp_path) -> None:
    store = StateStore(tmp_path / "nested" / "dir" / "state.json")
    assert store.save(PersistedState()) is True
    assert store.load() == PersistedState()


def test_corrupt_json_is_quarantined_and_conservative_state_returned(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    path.write_text("{this is not json", encoding="utf-8")
    store = StateStore(path)
    with caplog.at_level(logging.ERROR):
        state = store.load()
    assert state == CONSERVATIVE
    assert "corrupt" in caplog.text.lower()
    assert (tmp_path / "state.json.corrupt").exists()
    assert not path.exists()


def test_unknown_schema_version_is_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 99, "phase": "monitoring"}), encoding="utf-8")
    store = StateStore(path)
    with caplog.at_level(logging.ERROR):
        state = store.load()
    assert state == CONSERVATIVE
    assert (tmp_path / "state.json.corrupt").exists()


def test_invalid_field_types_are_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "phase": "monitoring", "outage_observed": "yes"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = StateStore(path).load()
    assert state == CONSERVATIVE


def test_invalid_pending_apps_is_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    payload = {
        "schema_version": 1,
        "phase": "recovering",
        "outage_observed": True,
        "pending_apps": "Sonarr",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = StateStore(path).load()
    assert state == CONSERVATIVE


def test_unknown_phase_is_treated_as_corrupt(tmp_path, caplog) -> None:
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "phase": "exploded", "outage_observed": False}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = StateStore(path).load()
    assert state == CONSERVATIVE


def test_save_failure_is_reported_not_raised(tmp_path, caplog, monkeypatch) -> None:
    # Force the atomic rename to fail instead of relying on directory
    # permissions, which root ignores.
    store = StateStore(tmp_path / "state.json")

    def deny_replace(src, dst) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("arr_recovery_watchdog.state.os.replace", deny_replace)
    with caplog.at_level(logging.ERROR):
        saved = store.save(PersistedState())
    assert saved is False
    assert "Failed to persist" in caplog.text


def test_verify_writable_succeeds_and_leaves_no_probe_file(tmp_path) -> None:
    store = StateStore(tmp_path / "nested" / "state.json")
    assert store.verify_writable() is True
    assert list((tmp_path / "nested").iterdir()) == []


def test_verify_writable_fails_on_unusable_state_directory(tmp_path, caplog) -> None:
    # A regular file where the state directory should be fails mkdir/open for
    # every user, root included — no permission tricks needed.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store = StateStore(blocker / "state.json")
    with caplog.at_level(logging.ERROR):
        writable = store.verify_writable()
    assert writable is False
    assert "not writable" in caplog.text
