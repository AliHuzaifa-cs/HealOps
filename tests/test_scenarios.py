"""End-to-end scenarios: a real fault on the toy app, through the whole loop."""

import pytest

from healops.agents.fallback import FallbackAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.audit import AuditStage
from healops.healing.notify import AlertKind
from healops.healing.orchestrator import HandleStatus
from healops.testing import COMPONENT, Fault, detect, heal, make_toy_system
from healops.types import IncidentType, PolicyVerdict

S = AuditStage


def stages(system):
    return [event.stage for event in system.audit.events]


def test_a_healthy_app_raises_no_incident():
    system = make_toy_system()
    assert detect(system, max_checks=5) == []


def test_one_odd_reading_is_not_enough_to_raise_an_incident():
    system = make_toy_system()
    system.app.inject(Fault.SLOW)
    assert detect(system, max_checks=1) == []  # needs 2 breaches in a row


@pytest.mark.parametrize(
    ("fault", "incident_type", "actions"),
    [
        (Fault.CRASH, IncidentType.HEALTH_FAILURE, [f"restart_component:{COMPONENT}"]),
        (Fault.SLOW, IncidentType.HIGH_LATENCY, [f"restart_component:{COMPONENT}"]),
        (
            Fault.MODEL_CORRUPT,
            IncidentType.HEALTH_FAILURE,
            [f"restart_component:{COMPONENT}", "reload_model"],
        ),
    ],
)
def test_a_fault_is_detected_and_healed_with_the_right_action(fault, incident_type, actions):
    system = make_toy_system()
    system.app.inject(fault)
    incidents = detect(system)
    assert [i.incident_type for i in incidents] == [incident_type]
    result = heal(system, incidents[0])
    assert result.status is HandleStatus.RESOLVED
    assert system.app.actions == actions
    assert system.app.faults == set()
    assert system.alerts.alerts == []


def test_when_the_first_action_does_not_help_the_next_one_is_tried():
    system = make_toy_system()
    system.app.inject(Fault.MODEL_CORRUPT)  # a restart does not fix this
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.RESOLVED
    assert system.app.actions[0].startswith("restart_component")
    assert system.app.actions[-1] == "reload_model"


def test_a_bad_model_is_rolled_back_and_both_of_its_incidents_resolve():
    system = make_toy_system()
    system.app.inject(Fault.BAD_MODEL)
    incidents = detect(system)
    assert {i.incident_type for i in incidents} == {
        IncidentType.HIGH_ERROR_RATE,
        IncidentType.PERFORMANCE_DEGRADATION,
    }
    results = [heal(system, incident) for incident in incidents]
    assert all(r.status is HandleStatus.RESOLVED for r in results)
    assert system.app.faults == set()
    assert system.app.actions[0] == "rollback_model"


def test_drift_cannot_be_fixed_automatically_so_a_person_is_alerted():
    system = make_toy_system()
    system.app.inject(Fault.DRIFT)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.ESCALATED
    assert system.app.actions == []
    assert system.app.faults == {Fault.DRIFT}
    assert [a.kind for a in system.alerts.alerts] == [AlertKind.ESCALATED]


def test_two_faults_at_once_are_both_healed_by_their_own_cure():
    system = make_toy_system()
    system.app.inject(Fault.CRASH)
    system.app.inject(Fault.SLOW)
    system.app.inject(Fault.BAD_MODEL)
    for incident in detect(system):
        heal(system, incident)
    # a second pass catches anything that only became visible after the first fix
    for incident in detect(system, max_checks=3):
        heal(system, incident)
    assert system.app.faults == set()


def test_dry_run_changes_nothing_and_alerts_a_person():
    system = make_toy_system(dry_run=True)
    system.app.inject(Fault.CRASH)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.ESCALATED
    assert system.app.actions == []
    assert system.app.faults == {Fault.CRASH}
    assert system.alerts.alerts[0].title.startswith("[DRY-RUN]")


def test_an_action_that_needs_approval_waits_for_a_person_then_runs():
    config = {
        "min_confidence_for_auto": 0.3,
        "thresholds": {"error_rate": 0.05, "consecutive_breaches": 2},
        "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
        "policies": {"rollback_model": {"verdict": "needs_approval", "cooldown_seconds": 0}},
    }
    system = make_toy_system(config)
    system.app.inject(Fault.BAD_MODEL)
    incident = detect(system)[0]
    parked = heal(system, incident)
    assert parked.status is HandleStatus.PARKED
    assert system.app.actions == []
    assert system.alerts.alerts[0].kind is AlertKind.APPROVAL_NEEDED

    ready = system.orchestrator.approve(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali"
    )
    done = system.orchestrator.verify(system.orchestrator.execute(ready))
    assert done.status is HandleStatus.RESOLVED
    assert system.app.actions == ["rollback_model"]


def test_a_rejected_action_is_never_run():
    config = {
        "min_confidence_for_auto": 0.3,
        "thresholds": {"error_rate": 0.05, "consecutive_breaches": 2},
        "policies": {"rollback_model": {"verdict": "needs_approval"}},
    }
    system = make_toy_system(config)
    system.app.inject(Fault.BAD_MODEL)
    parked = heal(system, detect(system)[0])
    rejected = system.orchestrator.reject(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali", "not now"
    )
    assert rejected.status is HandleStatus.ESCALATED
    assert system.app.actions == []


def test_a_denied_action_is_never_run():
    config = {
        "thresholds": {"error_rate": 0.05, "consecutive_breaches": 2},
        "policies": {"rollback_model": {"verdict": "deny"}},
    }
    system = make_toy_system(config)
    system.app.inject(Fault.BAD_MODEL)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.ESCALATED
    assert result.decision.verdict is PolicyVerdict.DENY
    assert system.app.actions == []


def test_with_the_llm_down_the_fallback_asks_a_person_instead_of_acting():
    class DownAgent:
        def diagnose(self, context):
            raise ConnectionError("llm unreachable")

    agent = FallbackAgent(DownAgent(), MockDiagnosisAgent(component=COMPONENT))
    system = make_toy_system(agent=agent)
    system.app.inject(Fault.CRASH)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.PARKED  # confidence 0.2 < 0.3 required
    assert system.app.actions == []
    alert_text = " ".join(system.alerts.alerts[0].details)
    assert "[FALLBACK: primary agent unavailable (ConnectionError)]" in alert_text


def test_a_broken_adapter_action_escalates_instead_of_crashing():
    system = make_toy_system()
    system.app.inject(Fault.CRASH)

    def failing_restart(component):
        raise OSError("cannot reach the host")

    system.app.restart_component = failing_restart  # type: ignore[method-assign]
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.ESCALATED
    assert "OSError" in result.reason
    assert system.app.faults == {Fault.CRASH}


def test_the_audit_trail_tells_the_whole_story_of_a_recovery():
    system = make_toy_system()
    system.app.inject(Fault.CRASH)
    heal(system, detect(system)[0])
    seen = stages(system)
    for stage in (
        S.DIAGNOSED,
        S.PROPOSED,
        S.POLICY_DECIDED,
        S.ACTION_STARTING,
        S.ACTION_FINISHED,
        S.VERIFIED,
        S.RESOLVED,
    ):
        assert stage in seen
    assert seen.index(S.ACTION_STARTING) < seen.index(S.ACTION_FINISHED) < seen.index(S.VERIFIED)
    assert seen[-1] is S.RESOLVED


def test_after_a_recovery_the_same_fault_can_be_detected_again():
    system = make_toy_system()
    system.app.inject(Fault.CRASH)
    assert heal(system, detect(system)[0]).status is HandleStatus.RESOLVED
    system.app.inject(Fault.CRASH)
    again = detect(system)
    assert [i.incident_type for i in again] == [IncidentType.HEALTH_FAILURE]