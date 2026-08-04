"""State machine tests: outage detection, stabilization, cooldown, recovery results, restarts."""

from __future__ import annotations

from arr_recovery_watchdog.machine import Action, Phase, RecoveryStateMachine
from arr_recovery_watchdog.state import PersistedState, StateStore

from .conftest import FakeClock

STABILITY = 60.0
COOLDOWN = 1800.0
POLL = 5.0
APPS = ("Prowlarr", "Sonarr", "Radarr")


def new_machine(
    clock: FakeClock,
    *,
    healthy: bool,
    persisted: PersistedState | None = None,
) -> RecoveryStateMachine:
    return RecoveryStateMachine.from_persisted(
        persisted or PersistedState(),
        stability_seconds=STABILITY,
        cooldown_seconds=COOLDOWN,
        app_names=APPS,
        healthy_now=healthy,
        mono_now=clock.monotonic(),
        utc_now=clock.now_utc(),
    )


def poll(machine: RecoveryStateMachine, clock: FakeClock, healthy: bool) -> Action | None:
    clock.advance(POLL)
    return machine.observe(healthy, clock.monotonic(), clock.now_utc())


def complete(machine: RecoveryStateMachine, clock: FakeClock, **results: bool) -> None:
    """Report recovery results; defaults to success for every pending app."""
    outcome = {name: results.get(name, True) for name in machine.pending_apps}
    machine.complete_recovery(outcome, clock.now_utc())


def run_one_recovery(machine: RecoveryStateMachine, clock: FakeClock) -> None:
    """Drive the machine through outage -> stable health -> one successful recovery."""
    assert poll(machine, clock, False) is None
    for _ in range(1000):
        if poll(machine, clock, True) is Action.RUN_RECOVERY:
            complete(machine, clock)
            return
    raise AssertionError("recovery never triggered")


def test_initial_healthy_startup_takes_no_action(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    assert machine.phase is Phase.MONITORING
    assert not machine.outage_observed
    assert not machine.dirty  # nothing changed, so no state write is needed
    for _ in range(20):
        assert poll(machine, clock, True) is None
    assert not machine.dirty


def test_initial_unhealthy_startup_records_outage(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=False)
    assert machine.phase is Phase.OUTAGE
    assert machine.outage_observed
    assert machine.dirty
    snapshot = machine.snapshot(clock.now_utc())
    assert snapshot.phase == "outage"
    assert snapshot.outage_observed is True


def test_outage_then_stable_health_triggers_exactly_one_recovery(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    assert poll(machine, clock, False) is None
    triggers = 0
    for _ in range(30):  # 150 s of healthy polls
        if poll(machine, clock, True) is Action.RUN_RECOVERY:
            triggers += 1
            complete(machine, clock)
    assert triggers == 1
    assert machine.phase is Phase.MONITORING


def test_trigger_enters_recovering_with_all_apps_pending(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.phase is Phase.RECOVERING
    assert machine.outage_observed  # the outage is not resolved until recovery completes
    assert machine.pending_apps == APPS
    snapshot = machine.snapshot(clock.now_utc())
    assert snapshot.phase == "recovering"
    assert snapshot.pending_apps == APPS


def test_successful_completion_resolves_the_outage(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    assert machine.phase is Phase.MONITORING
    assert not machine.outage_observed
    assert machine.pending_apps == ()


def test_recovering_reemits_action_until_completed(clock: FakeClock) -> None:
    """When the caller defers execution (e.g. state write failed), the action repeats."""
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    assert poll(machine, clock, True) is Action.RUN_RECOVERY
    assert poll(machine, clock, True) is Action.RUN_RECOVERY
    complete(machine, clock)
    assert poll(machine, clock, True) is None


def test_partial_failure_keeps_failed_apps_pending(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    complete(machine, clock, Sonarr=False)
    assert machine.phase is Phase.COOLDOWN_WAIT
    assert machine.outage_observed
    assert machine.pending_apps == ("Sonarr",)


def test_failed_apps_are_retried_after_the_cooldown(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    first_attempt = machine.last_recovery_utc
    assert first_attempt is not None
    complete(machine, clock, Radarr=False)

    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.pending_apps == ("Radarr",)  # only the failed app is retried
    assert machine.last_recovery_utc is not None
    assert (machine.last_recovery_utc - first_attempt).total_seconds() >= COOLDOWN
    complete(machine, clock)
    assert machine.phase is Phase.MONITORING
    assert not machine.outage_observed


def test_total_failure_retries_all_apps_after_the_cooldown(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    complete(machine, clock, Prowlarr=False, Sonarr=False, Radarr=False)
    assert machine.phase is Phase.COOLDOWN_WAIT
    assert machine.pending_apps == APPS

    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.pending_apps == APPS


def test_new_outage_resets_the_pending_set_to_all_apps(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    complete(machine, clock, Sonarr=False)
    assert machine.pending_apps == ("Sonarr",)

    poll(machine, clock, False)  # fresh outage supersedes the partial retry
    assert machine.phase is Phase.OUTAGE
    assert machine.pending_apps == ()
    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.pending_apps == APPS  # the full sequence runs again


def test_unhealthy_while_recovery_pending_returns_to_outage(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    while poll(machine, clock, True) is not Action.RUN_RECOVERY:
        pass
    assert machine.phase is Phase.RECOVERING
    assert poll(machine, clock, False) is None
    assert machine.phase is Phase.OUTAGE
    assert machine.outage_observed
    assert machine.pending_apps == ()


def test_failed_poll_resets_stabilization_timer(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    for _ in range(8):  # 35 s into stabilization
        assert poll(machine, clock, True) is None
    assert poll(machine, clock, False) is None  # resets the window
    assert machine.phase is Phase.OUTAGE
    # 55 s of health after the reset: still short of the 60 s requirement
    for _ in range(12):
        assert poll(machine, clock, True) is None
    # 60 s after the reset: recovery fires
    assert poll(machine, clock, True) is Action.RUN_RECOVERY


def test_repeated_healthy_polling_after_recovery_does_not_retrigger(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    for _ in range(500):  # 2500 s, far beyond the cooldown
        assert poll(machine, clock, True) is None
    assert machine.phase is Phase.MONITORING


def test_single_unhealthy_poll_is_remembered_until_stable_recovery(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    assert poll(machine, clock, False) is None  # one blip
    assert machine.outage_observed
    actions = [poll(machine, clock, True) for _ in range(13)]
    assert all(action is None for action in actions[:-1])
    assert actions[-1] is Action.RUN_RECOVERY


def test_cooldown_defers_second_recovery(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    poll(machine, clock, False)  # second outage right after the first recovery
    actions = [poll(machine, clock, True) for _ in range(13)]  # reaches stability again
    assert all(action is None for action in actions)
    assert machine.phase is Phase.COOLDOWN_WAIT


def test_deferred_recovery_executes_after_cooldown_expires(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    first_recovery_at = machine.last_recovery_utc
    assert first_recovery_at is not None

    poll(machine, clock, False)
    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.last_recovery_utc is not None
    elapsed = (machine.last_recovery_utc - first_recovery_at).total_seconds()
    assert elapsed >= COOLDOWN


def test_unhealthy_during_cooldown_wait_returns_to_outage(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    poll(machine, clock, False)
    for _ in range(13):
        poll(machine, clock, True)
    assert machine.phase is Phase.COOLDOWN_WAIT
    assert poll(machine, clock, False) is None
    assert machine.phase is Phase.OUTAGE
    assert machine.outage_observed


def test_no_state_writes_during_steady_stabilization_polling(clock: FakeClock) -> None:
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    assert machine.dirty
    machine.dirty = False
    poll(machine, clock, True)  # enters STABILIZING: meaningful change
    assert machine.dirty
    machine.dirty = False
    poll(machine, clock, True)  # mid-stabilization progress: no write needed
    assert not machine.dirty


def test_persisted_cooldown_survives_restart(clock: FakeClock, tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    machine = new_machine(clock, healthy=True)
    run_one_recovery(machine, clock)
    store.save(machine.snapshot(clock.now_utc()))

    clock.advance(100)  # process restart 100 s later
    restarted = new_machine(clock, healthy=True, persisted=store.load())
    assert restarted.phase is Phase.MONITORING
    assert not restarted.dirty  # nothing changed; restart must not rewrite state

    poll(restarted, clock, False)
    actions = [poll(restarted, clock, True) for _ in range(13)]
    assert all(action is None for action in actions)
    assert restarted.phase is Phase.COOLDOWN_WAIT  # persisted cooldown still enforced


def test_persisted_outage_survives_restart(clock: FakeClock, tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    machine = new_machine(clock, healthy=True)
    poll(machine, clock, False)
    store.save(machine.snapshot(clock.now_utc()))

    clock.advance(30)  # Gluetun recovered while the watchdog was down
    restarted = new_machine(clock, healthy=True, persisted=store.load())
    assert restarted.phase is Phase.STABILIZING  # prior outage remembered
    assert restarted.dirty

    actions = [poll(restarted, clock, True) for _ in range(12)]
    assert all(action is None for action in actions[:-1])
    assert actions[-1] is Action.RUN_RECOVERY


def test_restart_during_unresolved_outage_stays_in_outage(clock: FakeClock, tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    machine = new_machine(clock, healthy=False)
    store.save(machine.snapshot(clock.now_utc()))

    clock.advance(30)
    restarted = new_machine(clock, healthy=False, persisted=store.load())
    assert restarted.phase is Phase.OUTAGE
    assert restarted.outage_observed
    assert not restarted.dirty


def test_restart_mid_recovery_restabilizes_and_keeps_pending_apps(clock: FakeClock) -> None:
    """A crash while RECOVERING must not mark the recovery complete."""
    persisted = PersistedState(
        phase="recovering",
        outage_observed=True,
        pending_apps=("Sonarr", "Radarr"),
    )
    restarted = new_machine(clock, healthy=True, persisted=persisted)
    assert restarted.phase is Phase.STABILIZING
    assert restarted.outage_observed
    assert restarted.pending_apps == ("Sonarr", "Radarr")
    assert restarted.dirty

    action = None
    for _ in range(1000):
        action = poll(restarted, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert restarted.pending_apps == ("Sonarr", "Radarr")  # successful apps are not repeated


def test_restart_with_pending_retry_in_cooldown_wait_keeps_pending(clock: FakeClock) -> None:
    persisted = PersistedState(
        phase="cooldown_wait",
        outage_observed=True,
        pending_apps=("Sonarr",),
    )
    restarted = new_machine(clock, healthy=True, persisted=persisted)
    assert restarted.phase is Phase.STABILIZING
    assert restarted.pending_apps == ("Sonarr",)


def test_restart_mid_recovery_while_unhealthy_resets_pending(clock: FakeClock) -> None:
    persisted = PersistedState(
        phase="recovering",
        outage_observed=True,
        pending_apps=("Sonarr",),
    )
    restarted = new_machine(clock, healthy=False, persisted=persisted)
    assert restarted.phase is Phase.OUTAGE
    assert restarted.pending_apps == ()  # a fresh outage requires the full sequence


def test_persisted_pending_apps_not_configured_anymore_are_dropped(clock: FakeClock) -> None:
    persisted = PersistedState(
        phase="cooldown_wait",
        outage_observed=True,
        pending_apps=("Sonarr", "Lidarr"),
    )
    restarted = new_machine(clock, healthy=True, persisted=persisted)
    assert restarted.pending_apps == ("Sonarr",)


def test_conservative_fallback_state_causes_one_recovery(clock: FakeClock) -> None:
    """The corrupt-state fallback (assumed outage) recovers once after stability."""
    from arr_recovery_watchdog.state import conservative_fallback

    machine = new_machine(clock, healthy=True, persisted=conservative_fallback())
    assert machine.phase is Phase.STABILIZING
    action = None
    for _ in range(1000):
        action = poll(machine, clock, True)
        if action is not None:
            break
    assert action is Action.RUN_RECOVERY
    assert machine.pending_apps == APPS
