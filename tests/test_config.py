import pytest
from pydantic import ValidationError

from healops.config import ActionPolicy, HealOpsConfig
from healops.types import ActionType, PolicyVerdict


def test_empty_config_uses_safe_defaults():
    config = HealOpsConfig.model_validate({})
    assert config.dry_run is True
    assert config.default_policy.verdict is PolicyVerdict.NEEDS_APPROVAL
    assert config.thresholds.latency_ms is None


def test_full_config_is_parsed_from_plain_text_values():
    config = HealOpsConfig.model_validate(
        {
            "dry_run": False,
            "check_interval_seconds": 10,
            "thresholds": {"latency_ms": 500, "error_rate": 0.05},
            "policies": {
                "rollback_model": {
                    "verdict": "allow",
                    "max_retries": 2,
                    "cooldown_seconds": 600,
                }
            },
        }
    )
    assert config.dry_run is False
    assert config.thresholds.latency_ms == 500
    rollback = config.policy_for(ActionType.ROLLBACK_MODEL)
    assert rollback.verdict is PolicyVerdict.ALLOW
    assert rollback.max_retries == 2


def test_unlisted_action_falls_back_to_the_default_policy():
    config = HealOpsConfig.model_validate({})
    assert config.policy_for(ActionType.RELOAD_MODEL) == ActionPolicy()


def test_misspelled_key_is_rejected_instead_of_ignored():
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"thresholds": {"latency_msec": 500}})


def test_unknown_action_name_is_rejected():
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"policies": {"delete_everything": {}}})


@pytest.mark.parametrize(
    "bad",
    [
        {"thresholds": {"latency_ms": 0}},
        {"thresholds": {"error_rate": 1.5}},
        {"thresholds": {"drift_score": -1}},
        {"check_interval_seconds": 0},
        {"policies": {"reload_model": {"max_retries": -1}}},
        {"policies": {"reload_model": {"max_retries": 99}}},
        {"policies": {"reload_model": {"verdict": "maybe"}}},
    ],
)
def test_out_of_range_values_are_rejected(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate(bad)


def test_all_mistakes_are_reported_together():
    with pytest.raises(ValidationError) as info:
        HealOpsConfig.model_validate(
            {"thresholds": {"latency_ms": -5, "error_rate": 9}}
        )
    assert info.value.error_count() == 2


def test_redeployment_cannot_be_made_fully_automatic():
    with pytest.raises(ValidationError, match="request_redeployment"):
        HealOpsConfig.model_validate(
            {"policies": {"request_redeployment": {"verdict": "allow"}}}
        )


def test_redeployment_can_still_need_approval_or_be_denied():
    for verdict in ("needs_approval", "deny"):
        config = HealOpsConfig.model_validate(
            {"policies": {"request_redeployment": {"verdict": verdict}}}
        )
        assert config.policy_for(ActionType.REQUEST_REDEPLOYMENT).verdict == verdict


def test_default_policy_cannot_make_redeployment_automatic_either():
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"default_policy": {"verdict": "allow"}})


def test_consecutive_breaches_defaults_to_three():
    assert HealOpsConfig.model_validate({}).thresholds.consecutive_breaches == 3


@pytest.mark.parametrize("bad", [0, -1, 21])
def test_consecutive_breaches_must_be_between_one_and_twenty(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"thresholds": {"consecutive_breaches": bad}})