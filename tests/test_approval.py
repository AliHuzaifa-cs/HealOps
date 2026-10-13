from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.exceptions import (
    ApprovalError,
    ApprovalExpiredError,
    HealOpsError,
    NoPendingApprovalError,
)
from healops.healing.approval import TIMEOUT_APPROVER, ApprovalQueue
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal
from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
)

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def incident(state=IncidentState.AWAITING_APPROVAL):
    obs = Observation(
        app_name="demo-app", kind=ObservationKind.METRIC, name="latency_ms", value=950.0
    )
    return Incident(
        app_name="demo-app",
        incident_type=IncidentType.HIGH_LATENCY,
        severity=Severity.MEDIUM,
        summary="latency high",
        evidence=(obs,),
        state=state,
    )


def proposal_for(inc):
    return ActionProposal(
        incident_id=inc.incident_id,
        diagnosis_id="diag-1",
        action_type=ActionType.ROLLBACK_MODEL,
        rationale="because",
        expected_outcome="latency recovers",
    )


def decision_for(prop, verdict=PolicyVerdict.NEEDS_APPROVAL):
    return PolicyDecision(proposal_id=prop.proposal_id, verdict=verdict, reasons=("needs a person",))


def queue(clock=None, timeout=3600):
    return ApprovalQueue(timeout, clock=clock or FakeClock())


def parked(q, inc=None):
    inc = inc or incident()
    prop = proposal_for(inc)
    pending = q.add(inc, prop, decision_for(prop))
    return inc, prop, pending


def test_a_parked_incident_is_listed_with_its_deadline():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    inc, _, pending = parked(q)
    assert pending.requested_at == T0
    assert pending.expires_at == T0 + timedelta(seconds=600)
    assert q.get(inc.incident_id) is pending
    assert q.pending() == (pending,)
    assert len(q) == 1


def test_only_needs_approval_decisions_can_wait():
    q = queue()
    inc = incident()
    prop = proposal_for(inc)
    with pytest.raises(ApprovalError, match="NEEDS_APPROVAL"):
        q.add(inc, prop, decision_for(prop, PolicyVerdict.ALLOW))


def test_a_decision_for_another_proposal_is_refused():
    q = queue()
    inc = incident()
    prop, other = proposal_for(inc), proposal_for(inc)
    with pytest.raises(ApprovalError, match="proposal"):
        q.add(inc, prop, decision_for(other))


def test_a_proposal_for_another_incident_is_refused():
    q = queue()
    inc, other = incident(), incident()
    prop = proposal_for(other)
    with pytest.raises(ApprovalError, match="incident"):
        q.add(inc, prop, decision_for(prop))


def test_the_incident_must_be_in_the_awaiting_state():
    q = queue()
    inc = incident(IncidentState.PROPOSED)
    prop = proposal_for(inc)
    with pytest.raises(ApprovalError, match="awaiting_approval"):
        q.add(inc, prop, decision_for(prop))


def test_one_incident_cannot_wait_twice():
    q = queue()
    inc, _, _ = parked(q)
    prop = proposal_for(inc)
    with pytest.raises(ApprovalError, match="already waiting"):
        q.add(inc, prop, decision_for(prop))


def test_approving_resolves_it_and_records_who_approved():
    q = queue()
    inc, prop, pending = parked(q)
    resolution = q.approve(inc.incident_id, prop.proposal_id, "ali@example.com")
    assert resolution.approved is True
    assert resolution.approver == "ali@example.com"
    assert resolution.pending is pending
    assert len(q) == 0


def test_an_approval_cannot_be_used_twice():
    q = queue()
    inc, prop, _ = parked(q)
    q.approve(inc.incident_id, prop.proposal_id, "ali")
    with pytest.raises(NoPendingApprovalError):
        q.approve(inc.incident_id, prop.proposal_id, "ali")


def test_approving_a_different_proposal_is_refused_and_keeps_waiting():
    q = queue()
    inc, _, _ = parked(q)
    with pytest.raises(NoPendingApprovalError, match="different proposal"):
        q.approve(inc.incident_id, "some-other-proposal", "ali")
    assert len(q) == 1


def test_approving_another_incident_is_refused():
    q = queue()
    parked(q)
    with pytest.raises(NoPendingApprovalError):
        q.approve("unknown-incident", "unknown-proposal", "ali")


def test_the_approver_must_be_named():
    q = queue()
    inc, prop, _ = parked(q)
    with pytest.raises(ValueError):
        q.approve(inc.incident_id, prop.proposal_id, "  ")
    assert len(q) == 1


def test_approving_after_the_deadline_is_refused():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    inc, prop, _ = parked(q)
    clock.advance(600)
    with pytest.raises(ApprovalExpiredError):
        q.approve(inc.incident_id, prop.proposal_id, "ali")


def test_approving_just_before_the_deadline_works():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    inc, prop, _ = parked(q)
    clock.advance(599)
    assert q.approve(inc.incident_id, prop.proposal_id, "ali").approved is True


def test_rejecting_records_the_reason_and_is_never_an_approval():
    q = queue()
    inc, prop, _ = parked(q)
    resolution = q.reject(inc.incident_id, prop.proposal_id, "ali", "too risky tonight")
    assert resolution.approved is False
    assert resolution.reason == "too risky tonight"
    assert len(q) == 0


def test_a_rejection_cannot_be_repeated():
    q = queue()
    inc, prop, _ = parked(q)
    q.reject(inc.incident_id, prop.proposal_id, "ali")
    with pytest.raises(NoPendingApprovalError):
        q.reject(inc.incident_id, prop.proposal_id, "ali")


def test_rejecting_is_still_allowed_after_the_deadline():
    clock = FakeClock()
    q = queue(clock, timeout=60)
    inc, prop, _ = parked(q)
    clock.advance(120)
    assert q.reject(inc.incident_id, prop.proposal_id, "ali").approved is False


def test_nothing_expires_before_its_time():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    parked(q)
    clock.advance(599)
    assert q.expire_overdue() == []
    assert len(q) == 1


def test_overdue_approvals_are_handed_back_and_never_approved():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    parked(q)
    clock.advance(600)
    resolutions = q.expire_overdue()
    assert len(resolutions) == 1
    assert resolutions[0].approved is False
    assert resolutions[0].approver == TIMEOUT_APPROVER
    assert "timed out after 600 seconds" in resolutions[0].reason
    assert len(q) == 0


def test_only_the_overdue_ones_expire():
    clock = FakeClock()
    q = queue(clock, timeout=600)
    old, _, _ = parked(q)
    clock.advance(400)
    fresh, _, _ = parked(q)
    clock.advance(250)
    resolutions = q.expire_overdue()
    assert [r.pending.incident.incident_id for r in resolutions] == [old.incident_id]
    assert q.get(fresh.incident_id) is not None


def test_pending_lists_the_oldest_first():
    clock = FakeClock()
    q = queue(clock)
    first, _, _ = parked(q)
    clock.advance(10)
    second, _, _ = parked(q)
    assert [p.incident.incident_id for p in q.pending()] == [
        first.incident_id,
        second.incident_id,
    ]


def test_queues_do_not_share_state():
    first, second = queue(), queue()
    inc, _, _ = parked(first)
    assert second.get(inc.incident_id) is None


def test_timeout_must_be_at_least_one_second():
    with pytest.raises(ValueError):
        ApprovalQueue(0)


def test_approval_errors_share_one_base_class():
    assert issubclass(NoPendingApprovalError, ApprovalError)
    assert issubclass(ApprovalExpiredError, ApprovalError)
    assert issubclass(ApprovalError, HealOpsError)


def test_config_timeout_defaults_to_one_hour():
    assert HealOpsConfig.model_validate({}).approval_timeout_seconds == 3600


@pytest.mark.parametrize("bad", [0, -5, 604801])
def test_config_timeout_must_be_in_range(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"approval_timeout_seconds": bad})