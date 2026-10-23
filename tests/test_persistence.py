"""Restarting HealOps: what comes back, and what must never run twice."""

from datetime import UTC, datetime, timedelta

import pytest

from healops import HealOps
from healops.events import Incident, Observation
from healops.exceptions import NoPendingApprovalError, StateStoreError
from healops.healing.notify import RecordingNotifier
from healops.store import (
    ACKNOWLEDGED,
    ESCALATED,
    IN_PROGRESS,
    RESOLVED,
    StateStore,
)
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, Fault, ToyApp
from healops.types import IncidentState, IncidentType, ObservationKind, Severity

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
NEEDS_APPROVAL = {"restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0}}
DENIED = {"restart_component": {"verdict": "deny"}}


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def start(tmp_path, policies=None, clock=None, **options):
    """Start HealOps on files in tmp_path; calling it again is a restart."""
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": str(tmp_path / "state.db"),
        "approval_timeout_seconds": 600,
    }
    if policies:
        data["policies"] = {**DEFAULT_CONFIG["policies"], **policies}
    alerts = RecordingNotifier()
    kwargs = {"notifiers": [alerts], "sleep": lambda _s: None, **options}
    if clock is not None:
        kwargs["clock"] = clock
    system = HealOps(data, **kwargs)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    return system, app, alerts


def ticks(system, n=4):
    for _ in range(n):
        system.tick()


def test_a_waiting_approval_survives_a_restart_and_can_then_be_approved(tmp_path):
    first, app, _ = start(tmp_path, NEEDS_APPROVAL)
    app.inject(Fault.CRASH)
    ticks(first)
    waiting = first.pending_approvals()[0]
    first.close()

    second, app2, _ = start(tmp_path, NEEDS_APPROVAL)
    app2.inject(Fault.CRASH)
    assert second.restored.pending_approvals == 1
    [again] = second.pending_approvals()
    assert again.proposal.proposal_id == waiting.proposal.proposal_id
    assert again.expires_at == waiting.expires_at
    result = second.approve(again.incident.incident_id, again.proposal.proposal_id, "ali")
    assert result.incident.state is IncidentState.RESOLVED
    assert app2.actions == [f"restart_component:{COMPONENT}"]


def test_a_restored_approval_is_not_raised_again_by_the_detector(tmp_path):
    first, app, _ = start(tmp_path, NEEDS_APPROVAL)
    app.inject(Fault.CRASH)
    ticks(first)
    first.close()
    second, app2, _ = start(tmp_path, NEEDS_APPROVAL)
    app2.inject(Fault.CRASH)
    ticks(second, 6)
    assert second.stats.incidents == 0
    assert len(second.pending_approvals()) == 1


def test_an_approval_that_ran_out_while_down_is_escalated_never_approved(tmp_path):
    clock = Clock()
    first, app, _ = start(tmp_path, NEEDS_APPROVAL, clock=clock)
    app.inject(Fault.CRASH)
    ticks(first)
    first.close()

    clock.now = T0 + timedelta(hours=2)
    second, app2, _ = start(tmp_path, NEEDS_APPROVAL, clock=clock)
    app2.inject(Fault.CRASH)
    ticks(second, 2)
    assert second.pending_approvals() == ()
    assert app2.actions == []
    [stored] = second.store.incidents()
    assert stored.status == ESCALATED
    assert stored.incident.state is IncidentState.ESCALATED


def test_an_approval_that_was_answered_is_not_restored(tmp_path):
    first, app, _ = start(tmp_path, NEEDS_APPROVAL)
    app.inject(Fault.CRASH)
    ticks(first)
    waiting = first.pending_approvals()[0]
    first.reject(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali", "no")
    first.close()
    second, _app2, _ = start(tmp_path, NEEDS_APPROVAL)
    assert second.restored.pending_approvals == 0
    assert second.pending_approvals() == ()
    assert second.restored.escalated == 1


def test_an_escalated_incident_stays_open_and_is_not_duplicated(tmp_path):
    first, app, _ = start(tmp_path, DENIED)
    app.inject(Fault.CRASH)
    ticks(first)
    [open_before] = first.incidents().values()
    first.close()

    second, app2, _ = start(tmp_path, DENIED)
    app2.inject(Fault.CRASH)
    ticks(second, 6)
    assert second.restored.escalated == 1
    assert second.stats.incidents == 0
    assert list(second.incidents()) == [open_before.incident_id]


def test_acknowledging_lets_the_same_problem_be_found_again_and_is_remembered(tmp_path):
    first, app, _ = start(tmp_path, DENIED)
    app.inject(Fault.CRASH)
    ticks(first)
    [incident_id] = first.incidents()
    first.acknowledge(incident_id)
    assert first.store.incidents([ACKNOWLEDGED])
    first.close()

    second, app2, _ = start(tmp_path, DENIED)
    assert second.restored.escalated == 0
    assert second.incidents() == {}
    app2.inject(Fault.CRASH)
    ticks(second)
    assert second.stats.incidents == 1


def test_an_incident_that_was_mid_handling_is_escalated_not_retried(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    stuck = Incident(
        "toy",
        IncidentType.HEALTH_FAILURE,
        Severity.HIGH,
        "health check failed",
        (Observation("toy", ObservationKind.HEALTH, "health", False, T0),),
    )
    store.save_incident(stuck, IN_PROGRESS)
    store.close()

    system, app, alerts = start(tmp_path)
    app.inject(Fault.CRASH)
    assert system.restored.interrupted == 1
    assert app.actions == []
    [saved] = system.store.incidents()
    assert saved.status == ESCALATED
    assert saved.incident.state is IncidentState.ESCALATED
    assert "stopped" in (saved.reason or "")
    assert any("needs a person" in alert.title for alert in alerts.alerts)
    assert stuck.incident_id in system.incidents()


def test_an_approved_incident_that_was_interrupted_is_escalated_too(tmp_path):
    from healops.lifecycle import advance

    path = tmp_path / "state.db"
    store = StateStore(path)
    base = Incident(
        "toy",
        IncidentType.HEALTH_FAILURE,
        Severity.HIGH,
        "health check failed",
        (Observation("toy", ObservationKind.HEALTH, "health", False, T0),),
    )
    approved = base
    for state in (
        IncidentState.DIAGNOSED,
        IncidentState.PROPOSED,
        IncidentState.APPROVED,
    ):
        approved = advance(approved, state)
    store.save_incident(approved, IN_PROGRESS)
    store.close()

    system, app, _ = start(tmp_path)
    assert system.restored.interrupted == 1
    assert app.actions == []
    assert system.store.incidents([ESCALATED])[0].incident.state is IncidentState.ESCALATED


def test_a_resolved_incident_is_saved_and_not_restored(tmp_path):
    first, app, _ = start(tmp_path)
    app.inject(Fault.CRASH)
    ticks(first)
    assert first.stats.resolved == 1
    assert first.store.incidents([RESOLVED])
    first.close()
    second, _app2, _ = start(tmp_path)
    assert second.restored.escalated == 0
    assert second.restored.pending_approvals == 0
    assert second.incidents() == {}


def test_observations_are_saved_every_tick(tmp_path):
    system, _app, _ = start(tmp_path)
    ticks(system, 3)
    series = system.store.observations(name="health")
    assert len(series) == 3
    assert all(o.app_name == "toy" for o in series)


def test_without_a_state_file_everything_still_works_in_memory():
    data = {**DEFAULT_CONFIG, "dry_run": False, "audit_log_path": None, "state_db_path": None}
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    ticks(system)
    assert system.stats.resolved == 1
    assert system.store.path == ":memory:"
    assert system.store.incidents([RESOLVED])


class BrokenStore(StateStore):
    """A store that can be told to fail."""

    def __init__(self):
        super().__init__()
        self.fail = set()

    def _run(self, work):
        for name in self.fail:
            if name in work.__qualname__:
                raise StateStoreError("disk full")
        return super()._run(work)

    def save_pending(self, pending):
        if "save_pending" in self.fail:
            raise StateStoreError("disk full")
        super().save_pending(pending)

    def delete_pending(self, incident_id):
        if "delete_pending" in self.fail:
            raise StateStoreError("disk full")
        super().delete_pending(incident_id)

    def add_observations(self, observations):
        if "add_observations" in self.fail:
            raise StateStoreError("disk full")
        return super().add_observations(observations)

    def save_incident(self, *args, **kwargs):
        if "save_incident" in self.fail:
            raise StateStoreError("disk full")
        super().save_incident(*args, **kwargs)


def broken_system(policies=None):
    store = BrokenStore()
    data = {**DEFAULT_CONFIG, "dry_run": False, "audit_log_path": None, "state_db_path": None}
    if policies:
        data["policies"] = {**DEFAULT_CONFIG["policies"], **policies}
    system = HealOps(data, store=store, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    return system, app, store


def test_a_failing_store_does_not_stop_healing():
    system, app, store = broken_system()
    store.fail = {"add_observations", "save_incident"}
    app.inject(Fault.CRASH)
    reports = [system.tick() for _ in range(4)]
    assert system.stats.resolved == 1
    assert app.get_health() is True
    assert any("saving observations" in e for r in reports for e in r.errors)


def test_a_waiting_approval_still_works_when_it_cannot_be_saved():
    system, app, store = broken_system(NEEDS_APPROVAL)
    store.fail = {"save_pending"}
    app.inject(Fault.CRASH)
    ticks(system)
    assert len(system.pending_approvals()) == 1


def test_if_the_saved_approval_cannot_be_cleared_nothing_is_approved():
    system, app, store = broken_system(NEEDS_APPROVAL)
    app.inject(Fault.CRASH)
    ticks(system)
    waiting = system.pending_approvals()[0]
    store.fail = {"delete_pending"}
    with pytest.raises(StateStoreError):
        system.approve(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali")
    assert app.actions == []
    assert len(system.pending_approvals()) == 1
    store.fail = set()
    system.approve(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali")
    assert app.actions == [f"restart_component:{COMPONENT}"]


def test_approving_the_same_thing_twice_after_a_restart_is_impossible(tmp_path):
    first, app, _ = start(tmp_path, NEEDS_APPROVAL)
    app.inject(Fault.CRASH)
    ticks(first)
    waiting = first.pending_approvals()[0]
    first.approve(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali")
    first.close()
    second, _app2, _ = start(tmp_path, NEEDS_APPROVAL)
    with pytest.raises(NoPendingApprovalError):
        second.approve(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali")


class DyingApp(ToyApp):
    """Stands in for a process that is killed in the middle of an action."""

    def restart_component(self, component):
        raise KeyboardInterrupt


def test_a_crash_right_after_approval_is_not_lost_and_not_repeated(tmp_path):
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": str(tmp_path / "state.db"),
        "policies": {**DEFAULT_CONFIG["policies"], **NEEDS_APPROVAL},
    }
    first = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    app = DyingApp()
    first.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    ticks(first)
    waiting = first.pending_approvals()[0]
    with pytest.raises(KeyboardInterrupt):
        first.approve(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali")
    first.close()

    second, app2, _ = start(tmp_path, NEEDS_APPROVAL)
    assert second.restored.pending_approvals == 0
    assert second.restored.interrupted == 1
    assert app2.actions == []