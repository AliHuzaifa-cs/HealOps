import pytest

from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    PolicyVerdict,
    RiskLevel,
    Severity,
    VerificationOutcome,
)

ALL_ENUMS = [
    Severity,
    IncidentType,
    IncidentState,
    ActionType,
    RiskLevel,
    PolicyVerdict,
    VerificationOutcome,
]


@pytest.mark.parametrize("enum_cls", ALL_ENUMS)
def test_values_are_unique_lowercase_strings(enum_cls):
    values = [member.value for member in enum_cls]
    assert len(values) == len(set(values))
    assert all(isinstance(v, str) and v == v.lower() for v in values)


def test_enum_can_be_built_from_its_string_value():
    assert ActionType("reload_model") is ActionType.RELOAD_MODEL


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        ActionType("delete_everything")


def test_retraining_and_redeployment_are_different_actions():
    assert ActionType.REQUEST_RETRAINING != ActionType.REQUEST_REDEPLOYMENT


def test_executed_is_not_resolved():
    assert IncidentState.EXECUTED != IncidentState.RESOLVED