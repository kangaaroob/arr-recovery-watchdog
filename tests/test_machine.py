"""State machine tests: outage detection, stabilization, cooldown, restarts."""

from __future__ import annotations

from arr_recovery_watchdog.machine import Action, Phase, RecoveryStateMachine
from arr_recovery_watchdog.state import PersistedState, StateStore

from .conftest import FakeClock

STABILITY = 60.0
COOLDOWN = 1800.0
POLL = 5.0


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
        healthy_now=healthy,
        mono_now=clock.monotonic(),
        utc_now=clock.now_utc(),
    )


def poll(machine: RecoveryStateMachine, clock: FakeClock, healthy: bool) -> Action | None:
    clock.advance(POLL)
    return machine.observe(healthy, clock.monotonic(), clock.now_utc())


def run_one_recovery(machine: RecoveryStateMachine, clock: FakeClock) -> None:
    """Drive the machine through outage -> stable health -> a single recovery."""
    assert poll(machine, clock, False) is None
    for _ in range(1000):
        if poll(machine, clock, True) is Action.RUN_RECOVERY:
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
    actions = [poll(machine, clock, True) for _ in range(30)]  # 150 s of healthy polls
    assert actions.count(Action.RUN_RECOVERY) == 1
    assert machine.phase is Phase.MONITORING


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
