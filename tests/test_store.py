"""The SQLite state store: observations, incidents and waiting approvals."""

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from healops.events import Incident, Observation
from healops.exceptions import StateStoreError
from healops.healing.approval import PendingApproval
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal
from healops.store import (
    ESCALATED,
    IN_PROGRESS,
    SCHEMA_VERSION,
    StateStore,
)
from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
)

T0 = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)


def obs(value=1.0, name="latency_ms", at=T0, app="a", kind=ObservationKind.METRIC, **details):
    return Observation(app, kind, name, value, at, details)


def incident(state=IncidentState.OPEN, app="a"):
    return Incident(
        app,
        IncidentType.HIGH_LATENCY,
        Severity.HIGH,
        "latency high",
        (obs(900.0),),
        state=state,
    )


def pending(inc=None):
    inc = inc or incident(IncidentState.AWAITING_APPROVAL)
    proposal = ActionProposal(
        incident_id=inc.incident_id,
        diagnosis_id="d1",
        action_type=ActionType.RESTART_COMPONENT,
        rationale="restart it",
        expected_outcome="it recovers",
        params={"component": "model-server"},
    )
    decision = PolicyDecision(
        proposal_id=proposal.proposal_id,
        verdict=PolicyVerdict.NEEDS_APPROVAL,
        reasons=("policy says so",),
    )
    return PendingApproval(inc, proposal, decision, T0, T0 + timedelta(hours=1))


@pytest.fixture
def store():
    s = StateStore()
    yield s
    s.close()


def test_observations_round_trip_exactly(store):
    original = [obs(12.5, details_key="x"), obs(True, name="health", kind=ObservationKind.HEALTH)]
    assert store.add_observations(original) == 2
    assert store.observations() == original


def test_a_bool_stays_a_bool_and_a_number_stays_a_number(store):
    store.add_observations([obs(True, name="health", kind=ObservationKind.HEALTH), obs(1.0)])
    by_name = {o.name: o.value for o in store.observations()}
    assert by_name["health"] is True
    assert by_name["latency_ms"] == 1.0 and not isinstance(by_name["latency_ms"], bool)


def test_a_non_finite_value_comes_back_as_nan(store):
    store.add_observations([obs(float("nan")), obs(float("inf"), name="other")])
    values = [o.value for o in store.observations()]
    assert len(values) == 2
    assert all(math.isnan(v) for v in values)


def test_observations_come_back_oldest_first_and_limit_keeps_the_newest(store):
    times = [T0 + timedelta(seconds=i) for i in range(5)]
    store.add_observations([obs(float(i), at=t) for i, t in enumerate(times)])
    assert [o.value for o in store.observations()] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert [o.value for o in store.observations(limit=2)] == [3.0, 4.0]


def test_observations_can_be_filtered(store):
    store.add_observations(
        [
            obs(1.0, app="a", name="x"),
            obs(2.0, app="b", name="x"),
            obs(3.0, app="a", name="y", at=T0 + timedelta(hours=1)),
        ]
    )
    assert [o.value for o in store.observations(app_name="a")] == [1.0, 3.0]
    assert [o.value for o in store.observations(name="x")] == [1.0, 2.0]
    assert [o.value for o in store.observations(since=T0 + timedelta(minutes=1))] == [3.0]


def test_times_in_other_zones_are_compared_correctly(store):
    plus5 = timezone_plus5(T0)
    store.add_observations([obs(1.0, at=plus5)])
    assert store.observations(since=T0 - timedelta(seconds=1))[0].timestamp == T0
    assert store.observations(since=T0 + timedelta(seconds=1)) == []


def timezone_plus5(moment):
    from datetime import timezone

    return moment.astimezone(timezone(timedelta(hours=5)))


def test_empty_batches_and_bad_limits(store):
    assert store.add_observations([]) == 0
    with pytest.raises(ValueError):
        store.observations(limit=0)
    with pytest.raises(ValueError):
        store.incidents(limit=0)


def test_prune_removes_only_old_observations_and_keeps_incidents():
    s = StateStore(retention_days=7)
    s.add_observations([obs(1.0, at=T0 - timedelta(days=8)), obs(2.0, at=T0 - timedelta(days=1))])
    s.save_incident(incident(), IN_PROGRESS)
    assert s.prune(T0) == 1
    assert [o.value for o in s.observations()] == [2.0]
    assert len(s.incidents()) == 1


def test_retention_must_be_positive():
    with pytest.raises(ValueError):
        StateStore(retention_days=0)


def test_an_incident_is_saved_updated_and_read_back(store):
    inc = incident()
    store.save_incident(inc, IN_PROGRESS)
    store.save_incident(inc, ESCALATED, "needs a person")
    [stored] = store.incidents()
    assert stored.incident == inc
    assert stored.status == ESCALATED
    assert stored.reason == "needs a person"


def test_incidents_can_be_filtered_by_status(store):
    store.save_incident(incident(), IN_PROGRESS)
    store.save_incident(incident(), ESCALATED)
    assert len(store.incidents([ESCALATED])) == 1
    assert len(store.incidents([ESCALATED, IN_PROGRESS])) == 2
    assert store.incidents([]) == []


def test_an_unknown_status_is_refused(store):
    with pytest.raises(ValueError):
        store.save_incident(incident(), "mystery")


def test_a_waiting_approval_round_trips_and_can_be_deleted(store):
    waiting = pending()
    store.save_pending(waiting)
    assert store.pending() == [waiting]
    store.delete_pending(waiting.incident.incident_id)
    assert store.pending() == []


def test_deleting_something_that_is_not_there_is_harmless(store):
    store.delete_pending("nothing")


def test_state_survives_closing_and_reopening_the_file(tmp_path):
    path = tmp_path / "sub" / "state.db"
    first = StateStore(path)
    first.add_observations([obs(5.0)])
    first.save_pending(pending())
    first.close()
    second = StateStore(path)
    assert [o.value for o in second.observations()] == [5.0]
    assert len(second.pending()) == 1
    second.close()


def test_a_file_from_a_newer_version_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    db.commit()
    db.close()
    with pytest.raises(StateStoreError, match="newer"):
        StateStore(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    db.close()


def test_someone_elses_database_is_refused(tmp_path):
    path = tmp_path / "other.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE people (name TEXT)")
    db.commit()
    db.close()
    with pytest.raises(StateStoreError, match="not a HealOps"):
        StateStore(path)


def test_a_file_that_is_not_a_database_gives_a_clean_error(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(StateStoreError) as caught:
        StateStore(path)
    assert "this is not sqlite" not in str(caught.value)


def test_a_closed_store_raises_a_store_error(store):
    store.close()
    with pytest.raises(StateStoreError):
        store.add_observations([obs()])


def test_two_threads_can_write_at_once(store):
    import threading

    def write(n):
        for i in range(50):
            store.add_observations([obs(float(i), name=f"m{n}")])

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.observations(limit=1000)) == 200


def test_decisions_are_recorded_in_order_and_finished_once(store):
    first = store.add_decision("inc1", "p1", "approve", "  ali  ", now=T0)
    second = store.add_decision("inc2", "p2", "reject", "sara", "no", now=T0)
    waiting = store.waiting_decisions()
    assert [d.decision_id for d in waiting] == [first, second]
    assert waiting[0].approver == "ali" and waiting[1].reason == "no"
    store.finish_decision(first, "applied", "resolved", now=T0)
    store.finish_decision(first, "failed", "too late", now=T0)  # a second finish changes nothing
    done = store.decision(first)
    assert (done.status, done.outcome) == ("applied", "resolved")
    assert [d.decision_id for d in store.waiting_decisions()] == [second]
    assert store.decision(999) is None


def test_decisions_are_validated(store):
    with pytest.raises(ValueError):
        store.add_decision("i", "p", "delete", "ali")
    with pytest.raises(ValueError):
        store.add_decision("i", "p", "approve", " ")
    store.add_decision("i", "p", "approve", "ali")
    with pytest.raises(ValueError, match="already waiting"):
        store.add_decision("i", "p", "reject", "ali")
    store.finish_decision(1, "applied", "x")
    store.add_decision("i", "p", "reject", "ali")  # fine once the first one is finished
    with pytest.raises(ValueError):
        store.finish_decision(1, "waiting", "x")


def test_the_heartbeat_round_trips(store):
    assert store.last_beat() is None
    store.beat(T0)
    store.beat(T0 + timedelta(seconds=5))
    assert store.last_beat() == T0 + timedelta(seconds=5)


def test_an_older_state_file_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, app_name TEXT NOT NULL, "
               "status TEXT NOT NULL, reason TEXT, incident_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    upgraded = StateStore(path)
    upgraded.add_decision("i", "p", "approve", "ali")
    upgraded.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_alerts_round_trip_newest_first(store):
    from healops.healing.notify import Alert, AlertKind

    def make(n, dry=False):
        return Alert(
            kind=AlertKind.ESCALATED,
            incident_id=f"inc{n}",
            app_name="a",
            title=f"title {n}",
            details=("one", "two"),
            created_at=T0 + timedelta(minutes=n),
            incident_type="high_latency",
            dry_run=dry,
        )

    for n in range(3):
        store.add_alert(make(n, dry=n == 1))
    found = store.alerts()
    assert [a.title for a in found] == ["title 2", "title 1", "title 0"]
    assert found[1].dry_run is True and found[0].dry_run is False
    assert found[0].details == ("one", "two") and found[0].incident_type == "high_latency"
    assert [a.title for a in store.alerts(limit=1)] == ["title 2"]
    assert [a.title for a in store.alerts(since=T0 + timedelta(minutes=1))] == ["title 2", "title 1"]
    with pytest.raises(ValueError):
        store.alerts(limit=0)