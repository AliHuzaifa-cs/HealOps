import pytest
from pydantic import ValidationError

from healops.config import HealOpsConfig
from healops.healing.policy import PolicyEngine
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, PolicyVerdict

ALL_ACTIONS = frozenset(ActionType)


def config(**overrides):
    values = {
        "policies": {
            "reload_model": {"verdict": "allow"},
            "rollback_model": {"verdict": "needs_approval"},
            "request_retraining": {"verdict": "deny"},
        }
    }
    values.update(overrides)
    return HealOpsConfig.model_validate(values)


def diagnosis(confidence=0.9, cause="model is slower"):
    return Diagnosis(
        incident_id="inc-1",
        summary="slow after reload",
        confidence=confidence,
        probable_cause=cause,
        evidence_used=("latency_ms = 950.0",) if cause else (),
        missing_information=() if cause else ("raw error logs",),
    )


def proposal(diag, action):
    return ActionProposal(
        incident_id=diag.incident_id,
        diagnosis_id=diag.diagnosis_id,
        action_type=action,
        rationale="because",
        expected_outcome="latency recovers",
    )


def decide(action, diag=None, cfg=None, available=ALL_ACTIONS):
    diag = diag or diagnosis()
    engine = PolicyEngine(cfg or config())
    return engine.evaluate(proposal(diag, action), diag, available)


def test_config_allow_with_high_confidence_is_allowed():
    decision = decide(ActionType.RELOAD_MODEL)
    assert decision.verdict is PolicyVerdict.ALLOW


def test_config_needs_approval_stays_needs_approval():
    assert decide(ActionType.ROLLBACK_MODEL).verdict is PolicyVerdict.NEEDS_APPROVAL


def test_config_deny_is_denied_with_a_reason():
    decision = decide(ActionType.REQUEST_RETRAINING)
    assert decision.verdict is PolicyVerdict.DENY
    assert "configuration denies request_retraining" in decision.reasons


def test_unlisted_action_uses_the_default_policy():
    assert decide(ActionType.RESTART_COMPONENT).verdict is PolicyVerdict.NEEDS_APPROVAL


def test_an_action_the_application_cannot_do_is_denied():
    available = ALL_ACTIONS - {ActionType.RELOAD_MODEL}
    decision = decide(ActionType.RELOAD_MODEL, available=available)
    assert decision.verdict is PolicyVerdict.DENY
    assert any("not supported" in reason for reason in decision.reasons)


def test_a_proposal_from_another_diagnosis_is_denied():
    diag, other = diagnosis(), diagnosis()
    engine = PolicyEngine(config())
    decision = engine.evaluate(proposal(other, ActionType.RELOAD_MODEL), diag, ALL_ACTIONS)
    assert decision.verdict is PolicyVerdict.DENY


def test_a_risky_action_without_a_probable_cause_is_denied():
    decision = decide(ActionType.RELOAD_MODEL, diag=diagnosis(cause=None))
    assert decision.verdict is PolicyVerdict.DENY
    assert any("probable cause" in reason for reason in decision.reasons)


def test_every_denial_reason_is_recorded_together():
    available = ALL_ACTIONS - {ActionType.REQUEST_RETRAINING}
    decision = decide(
        ActionType.REQUEST_RETRAINING, diag=diagnosis(cause=None), available=available
    )
    assert decision.verdict is PolicyVerdict.DENY
    assert len(decision.reasons) == 3


def test_low_confidence_turns_allow_into_needs_approval():
    decision = decide(ActionType.RELOAD_MODEL, diag=diagnosis(confidence=0.4))
    assert decision.verdict is PolicyVerdict.NEEDS_APPROVAL
    assert any("confidence 0.40 is below 0.60" in reason for reason in decision.reasons)


def test_confidence_exactly_at_the_threshold_is_enough():
    decision = decide(ActionType.RELOAD_MODEL, diag=diagnosis(confidence=0.6))
    assert decision.verdict is PolicyVerdict.ALLOW


def test_high_confidence_never_upgrades_needs_approval():
    decision = decide(ActionType.ROLLBACK_MODEL, diag=diagnosis(confidence=1.0))
    assert decision.verdict is PolicyVerdict.NEEDS_APPROVAL


def test_high_confidence_never_overrides_a_config_deny():
    decision = decide(ActionType.REQUEST_RETRAINING, diag=diagnosis(confidence=1.0))
    assert decision.verdict is PolicyVerdict.DENY


def test_the_confidence_threshold_comes_from_config():
    cfg = config(min_confidence_for_auto=0.3)
    decision = decide(ActionType.RELOAD_MODEL, diag=diagnosis(confidence=0.4), cfg=cfg)
    assert decision.verdict is PolicyVerdict.ALLOW


@pytest.mark.parametrize(
    "action", [ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE, ActionType.NO_ACTION]
)
def test_framework_actions_are_always_allowed_even_with_low_confidence(action):
    decision = decide(action, diag=diagnosis(confidence=0.0, cause=None))
    assert decision.verdict is PolicyVerdict.ALLOW


def test_escalation_is_allowed_even_under_a_strict_default_policy():
    cfg = config(default_policy={"verdict": "deny"})
    assert decide(ActionType.ESCALATE, cfg=cfg).verdict is PolicyVerdict.ALLOW


def test_redeployment_is_never_allowed_automatically():
    cfg = config(policies={"request_redeployment": {"verdict": "needs_approval"}})
    decision = decide(ActionType.REQUEST_REDEPLOYMENT, cfg=cfg)
    assert decision.verdict is PolicyVerdict.NEEDS_APPROVAL


def test_decision_refers_to_the_proposal_and_has_reasons():
    diag = diagnosis()
    prop = proposal(diag, ActionType.RELOAD_MODEL)
    decision = PolicyEngine(config()).evaluate(prop, diag, ALL_ACTIONS)
    assert decision.proposal_id == prop.proposal_id
    assert decision.reasons


def test_same_input_always_gives_the_same_verdict():
    diag = diagnosis(confidence=0.4)
    engine = PolicyEngine(config())
    prop = proposal(diag, ActionType.RELOAD_MODEL)
    verdicts = {engine.evaluate(prop, diag, ALL_ACTIONS).verdict for _ in range(5)}
    assert verdicts == {PolicyVerdict.NEEDS_APPROVAL}


def test_config_rejects_a_policy_for_a_framework_action():
    with pytest.raises(ValidationError, match="handled by HealOps itself"):
        HealOpsConfig.model_validate({"policies": {"escalate": {"verdict": "deny"}}})


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_confidence_threshold_must_be_between_zero_and_one(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"min_confidence_for_auto": bad})


def test_confidence_threshold_defaults_to_point_six():
    assert HealOpsConfig.model_validate({}).min_confidence_for_auto == 0.6