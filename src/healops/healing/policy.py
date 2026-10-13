"""The deterministic policy engine: the only thing that can permit an action.

An agent (mock or LLM) only proposes. This engine decides ALLOW, NEEDS_APPROVAL
or DENY from fixed rules and records every reason. It never calls an LLM.
"""

from healops.capabilities import FRAMEWORK_ACTIONS
from healops.config import HealOpsConfig
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, PolicyVerdict


class PolicyEngine:
    """Validates one ActionProposal against the application's abilities and the config."""

    def __init__(self, config: HealOpsConfig) -> None:
        self._config = config

    def evaluate(
        self,
        proposal: ActionProposal,
        diagnosis: Diagnosis,
        available_actions: frozenset[ActionType],
    ) -> PolicyDecision:
        action = proposal.action_type
        denials: list[str] = []

        if proposal.diagnosis_id != diagnosis.diagnosis_id:
            denials.append("the proposal does not belong to this diagnosis")
        if action not in available_actions:
            denials.append(f"{action.value} is not supported by the application")
        if diagnosis.probable_cause is None and action not in FRAMEWORK_ACTIONS:
            denials.append(
                f"{action.value} was proposed without a probable cause; "
                "only run_diagnostic, escalate or no_action are allowed"
            )

        # HealOps' own actions are always safe to run, and escalating to a person
        # must never be blocked or wait for approval.
        if action in FRAMEWORK_ACTIONS:
            if denials:
                return self._decision(proposal, PolicyVerdict.DENY, denials)
            return self._decision(
                proposal, PolicyVerdict.ALLOW, [f"{action.value} is a safe framework action"]
            )

        policy = self._config.policy_for(action)
        if policy.verdict is PolicyVerdict.DENY:
            denials.append(f"configuration denies {action.value}")
        if denials:
            return self._decision(proposal, PolicyVerdict.DENY, denials)

        reasons = [f"configuration verdict for {action.value} is {policy.verdict.value}"]
        verdict = policy.verdict
        threshold = self._config.min_confidence_for_auto
        if verdict is PolicyVerdict.ALLOW and diagnosis.confidence < threshold:
            verdict = PolicyVerdict.NEEDS_APPROVAL
            reasons.append(
                f"confidence {diagnosis.confidence:.2f} is below {threshold:.2f}, "
                "so a person must approve"
            )
        return self._decision(proposal, verdict, reasons)

    @staticmethod
    def _decision(
        proposal: ActionProposal, verdict: PolicyVerdict, reasons: list[str]
    ) -> PolicyDecision:
        return PolicyDecision(
            proposal_id=proposal.proposal_id, verdict=verdict, reasons=tuple(reasons)
        )