import pytest

from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType


def make_diagnosis(**overrides):
    values = {
        "incident_id": "inc-1",
        "summary": "Latency rose after a model reload",
        "confidence": 0.7,
        "probable_cause": "New model version is slower",
        "evidence_used": ("latency_ms rose from 120 to 950",),
    }
    values.update(overrides)
    return Diagnosis(**values)


def make_proposal(**overrides):
    values = {
        "incident_id": "inc-1",
        "diagnosis_id": "diag-1",
        "action_type": ActionType.ROLLBACK_MODEL,
        "rationale": "Latency regressed right after the last deployment",
        "expected_outcome": "Latency returns below the threshold",
    }
    values.update(overrides)
    return ActionProposal(**values)


def test_diagnosis_with_cause_and_evidence_is_valid():
    diagnosis = make_diagnosis()
    assert diagnosis.probable_cause is not None
    assert diagnosis.diagnosis_id


def test_cause_without_evidence_is_rejected():
    with pytest.raises(ValueError):
        make_diagnosis(evidence_used=())


def test_agent_can_abstain_if_it_lists_missing_information():
    diagnosis = make_diagnosis(
        probable_cause=None,
        evidence_used=(),
        confidence=0.2,
        missing_information=("no error logs were supplied",),
    )
    assert diagnosis.probable_cause is None


def test_abstaining_without_saying_why_is_rejected():
    with pytest.raises(ValueError):
        make_diagnosis(probable_cause=None, evidence_used=())


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan")])
def test_confidence_must_be_between_zero_and_one(bad):
    with pytest.raises(ValueError):
        make_diagnosis(confidence=bad)


def test_proposal_accepts_a_known_action():
    proposal = make_proposal()
    assert proposal.action_type is ActionType.ROLLBACK_MODEL
    assert proposal.params == {}


def test_proposal_rejects_an_action_given_as_raw_text():
    with pytest.raises(TypeError):
        make_proposal(action_type="delete_everything")
    with pytest.raises(TypeError):
        make_proposal(action_type="rollback_model")


def test_proposal_needs_rationale_and_expected_outcome():
    with pytest.raises(ValueError):
        make_proposal(rationale=" ")
    with pytest.raises(ValueError):
        make_proposal(expected_outcome="")


def test_proposal_ids_are_unique():
    assert make_proposal().proposal_id != make_proposal().proposal_id