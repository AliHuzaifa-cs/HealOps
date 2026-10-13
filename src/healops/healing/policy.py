"""The deterministic policy engine: the only thing that can permit an action.

An agent (mock or LLM) only proposes. This engine decides ALLOW, NEEDS_APPROVAL
or DENY from fixed rules and records every reason. It never calls an LLM.
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime

from healops.capabilities import FRAMEWORK_ACTIONS
from healops.config import ActionPolicy, HealOpsConfig
from healops.healing.history import ActionHistory
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, PolicyVerdict

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PolicyEngine:
    """Validates one ActionProposal against the application's abilities and the config.

    With an ActionHistory it also enforces retry limits and cooldowns. `max_retries`
    counts retries after the first run, so an action may run 1 + max_retries times
    per incident. The cooldown applies per application and action, across incidents.
    """

    def __init__(
        self,
        config: HealOpsConfig,
        history: ActionHistory | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._config = config
        self._history = history
        self._clock = clock

    def evaluate(
        self,
        proposal: ActionProposal,
        diagnosis: Diagnosis,
        available_actions: frozenset[ActionType],
        app_name: str | None = None,
    ) -> PolicyDecision:
        if self._history is not None and app_name is None:
            raise ValueError("app_name is required when the policy engine has a history")

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
        if self._history is not None and app_name is not None:
            denials.extend(self._limit_denials(proposal, action, policy, app_name))
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

    def _limit_denials(
        self,
        proposal: ActionProposal,
        action: ActionType,
        policy: ActionPolicy,
        app_name: str,
    ) -> list[str]:
        history = self._history
        assert history is not None
        denials: list[str] = []

        allowed_runs = 1 + policy.max_retries
        runs = history.attempts(proposal.incident_id, action)
        if runs >= allowed_runs:
            denials.append(
                f"{action.value} already ran {runs} time(s) for this incident "
                f"(limit is {allowed_runs}: 1 run + {policy.max_retries} retries)"
            )

        last = history.last_executed(app_name, action)
        if last is not None:
            elapsed = (self._clock() - last).total_seconds()
            if elapsed < policy.cooldown_seconds:
                remaining = math.ceil(policy.cooldown_seconds - elapsed)
                denials.append(
                    f"cooldown: {action.value} ran {int(elapsed)}s ago, "
                    f"{remaining}s remain of {policy.cooldown_seconds}s"
                )
        return denials

    @staticmethod
    def _decision(
        proposal: ActionProposal, verdict: PolicyVerdict, reasons: list[str]
    ) -> PolicyDecision:
        return PolicyDecision(
            proposal_id=proposal.proposal_id, verdict=verdict, reasons=tuple(reasons)
        )