from datetime import UTC, datetime, timedelta

import pytest

from healops.config import HealOpsConfig
from healops.healing.history import ActionHistory
from healops.healing.policy import PolicyEngine
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, PolicyVerdict

ALL_ACTIONS = frozenset(ActionType)
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def config(max_retries=1, cooldown_seconds=300):
    return HealOpsConfig.model_validate(
        {
            "policies": {
                "reload_model": {
                    "verdict": "allow",
                    "max_retries": max_retries,
                    "cooldown_seconds": cooldown_seconds,
                }
            }
        }
    )


def diagnosis():
    return Diagnosis(
        incident_id="inc-1",
        summary="slow",
        confidence=0.9,
        probable_cause="model is slower",
        evidence_used=("latency_ms = 950.0",),
    )


def proposal(diag, action=ActionType.RELOAD_MODEL):
    return ActionProposal(
        incident_id="inc-1",
        diagnosis_id=diag.diagnosis_id,
        action_type=action,
        rationale="because",
        expected_outcome="latency recovers",
    )


def decide(history, cfg=None, action=ActionType.RELOAD_MODEL, app="demo-app", now=NOW):
    diag = diagnosis()
    engine = PolicyEngine(cfg or config(), history=history, clock=lambda: now)
    return engine.evaluate(proposal(diag, action), diag, ALL_ACTIONS, app_name=app)


def ran(history, minutes_ago, incident="inc-1", app="demo-app", action=ActionType.RELOAD_MODEL):
    history.record(app, incident, action, NOW - timedelta(minutes=minutes_ago))


def test_first_run_is_allowed():
    assert decide(ActionHistory()).verdict is PolicyVerdict.ALLOW


def test_one_retry_is_allowed_by_default():
    history = ActionHistory()
    ran(history, minutes_ago=10)
    assert decide(history).verdict is PolicyVerdict.ALLOW


def test_the_run_after_the_retry_limit_is_denied():
    history = ActionHistory()
    ran(history, minutes_ago=20)
    ran(history, minutes_ago=10)
    decision = decide(history)
    assert decision.verdict is PolicyVerdict.DENY
    assert any("limit is 2" in reason for reason in decision.reasons)


def test_zero_retries_means_a_single_run():
    history = ActionHistory()
    ran(history, minutes_ago=10)
    assert decide(history, cfg=config(max_retries=0)).verdict is PolicyVerdict.DENY


def test_runs_in_another_incident_do_not_use_up_this_incidents_retries():
    history = ActionHistory()
    ran(history, minutes_ago=20, incident="inc-0")
    ran(history, minutes_ago=10, incident="inc-0")
    assert decide(history).verdict is PolicyVerdict.ALLOW


def test_cooldown_blocks_a_run_that_is_too_soon():
    history = ActionHistory()
    history.record("demo-app", "inc-1", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=100))
    decision = decide(history)
    assert decision.verdict is PolicyVerdict.DENY
    assert any("cooldown" in reason and "200s remain" in reason for reason in decision.reasons)


def test_cooldown_ends_exactly_when_the_time_has_passed():
    history = ActionHistory()
    history.record("demo-app", "inc-1", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=300))
    assert decide(history).verdict is PolicyVerdict.ALLOW


def test_cooldown_also_applies_across_incidents():
    history = ActionHistory()
    history.record("demo-app", "inc-0", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=30))
    assert decide(history).verdict is PolicyVerdict.DENY


def test_cooldown_is_per_application():
    history = ActionHistory()
    history.record("other-app", "inc-0", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=30))
    assert decide(history).verdict is PolicyVerdict.ALLOW


def test_cooldown_is_per_action():
    history = ActionHistory()
    history.record("demo-app", "inc-0", ActionType.ROLLBACK_MODEL, NOW - timedelta(seconds=30))
    assert decide(history).verdict is PolicyVerdict.ALLOW


def test_zero_cooldown_never_blocks():
    history = ActionHistory()
    history.record("demo-app", "inc-1", ActionType.RELOAD_MODEL, NOW)
    assert decide(history, cfg=config(cooldown_seconds=0)).verdict is PolicyVerdict.ALLOW


def test_both_limits_are_reported_together():
    history = ActionHistory()
    history.record("demo-app", "inc-1", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=20))
    history.record("demo-app", "inc-1", ActionType.RELOAD_MODEL, NOW - timedelta(seconds=10))
    decision = decide(history)
    assert decision.verdict is PolicyVerdict.DENY
    assert len(decision.reasons) == 2


@pytest.mark.parametrize(
    "action", [ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE, ActionType.NO_ACTION]
)
def test_framework_actions_ignore_limits(action):
    history = ActionHistory()
    for _ in range(5):
        history.record("demo-app", "inc-1", action, NOW)
    assert decide(history, action=action).verdict is PolicyVerdict.ALLOW


def test_limits_do_not_apply_without_a_history():
    diag = diagnosis()
    engine = PolicyEngine(config())
    decision = engine.evaluate(proposal(diag), diag, ALL_ACTIONS)
    assert decision.verdict is PolicyVerdict.ALLOW


def test_a_history_needs_the_application_name():
    diag = diagnosis()
    engine = PolicyEngine(config(), history=ActionHistory())
    with pytest.raises(ValueError, match="app_name"):
        engine.evaluate(proposal(diag), diag, ALL_ACTIONS)