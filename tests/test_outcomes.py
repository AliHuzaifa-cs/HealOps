from datetime import UTC, datetime

import pytest

from healops.outcomes import ActionResult, PolicyDecision, VerificationResult
from healops.types import PolicyVerdict, VerificationOutcome

T1 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 10, 12, 0, 5, tzinfo=UTC)


def make_decision(**overrides):
    values = {
        "proposal_id": "prop-1",
        "verdict": PolicyVerdict.ALLOW,
        "reasons": ("action is allowlisted and low risk",),
    }
    values.update(overrides)
    return PolicyDecision(**values)


def make_result(**overrides):
    values = {
        "proposal_id": "prop-1",
        "dry_run": False,
        "completed": True,
        "message": "model reloaded",
        "started_at": T1,
        "ended_at": T2,
    }
    values.update(overrides)
    return ActionResult(**values)


def make_verification(**overrides):
    values = {
        "execution_id": "exec-1",
        "outcome": VerificationOutcome.VERIFIED,
        "checks_passed": ("health check ok", "latency below threshold"),
    }
    values.update(overrides)
    return VerificationResult(**values)


def test_decision_needs_a_reason():
    with pytest.raises(ValueError):
        make_decision(reasons=())


def test_decision_rejects_a_verdict_given_as_raw_text():
    with pytest.raises(TypeError):
        make_decision(verdict="allow")


def test_valid_decision_has_id_and_timestamp():
    decision = make_decision(verdict=PolicyVerdict.DENY)
    assert decision.decision_id
    assert decision.decided_at.tzinfo is not None


def test_result_rejects_end_before_start():
    with pytest.raises(ValueError):
        make_result(started_at=T2, ended_at=T1)


def test_result_needs_a_message():
    with pytest.raises(ValueError):
        make_result(message=" ")


def test_result_can_record_a_dry_run_and_a_failed_callback():
    result = make_result(dry_run=True, completed=False, message="callback raised")
    assert result.dry_run is True
    assert result.completed is False


def test_verified_needs_passed_checks():
    with pytest.raises(ValueError):
        make_verification(checks_passed=())


def test_verified_cannot_have_failed_checks():
    with pytest.raises(ValueError):
        make_verification(checks_failed=("error rate still high",))


def test_failed_needs_a_failed_check():
    with pytest.raises(ValueError):
        make_verification(outcome=VerificationOutcome.FAILED, checks_passed=())


def test_partial_needs_both_kinds_of_checks():
    with pytest.raises(ValueError):
        make_verification(outcome=VerificationOutcome.PARTIAL)
    partial = make_verification(
        outcome=VerificationOutcome.PARTIAL,
        checks_failed=("latency still above threshold",),
    )
    assert partial.outcome is VerificationOutcome.PARTIAL


def test_inconclusive_needs_no_checks():
    result = make_verification(
        outcome=VerificationOutcome.INCONCLUSIVE, checks_passed=()
    )
    assert result.outcome is VerificationOutcome.INCONCLUSIVE


def test_outcome_given_as_raw_text_is_rejected():
    with pytest.raises(TypeError):
        make_verification(outcome="verified")