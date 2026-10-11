"""Validated configuration for HealOps.

Configuration is outside data written by a person, so it is validated at the
boundary with Pydantic. Unknown keys are rejected so a typo such as
`latency_msec` cannot silently disable a threshold.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from healops.types import ActionType, PolicyVerdict


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ThresholdsConfig(_StrictModel):
    """Limits that turn an observation into an incident. None means 'not monitored'."""

    latency_ms: float | None = Field(default=None, gt=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    drift_score: float | None = Field(default=None, ge=0)
    consecutive_breaches: int = Field(default=3, ge=1, le=20)


class ActionPolicy(_StrictModel):
    """How one kind of recovery action may be used."""

    verdict: PolicyVerdict = PolicyVerdict.NEEDS_APPROVAL
    max_retries: int = Field(default=1, ge=0, le=10)
    cooldown_seconds: int = Field(default=300, ge=0)


class HealOpsConfig(_StrictModel):
    """Top-level configuration. Every default is the safe choice."""

    dry_run: bool = True
    check_interval_seconds: int = Field(default=30, ge=1)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    default_policy: ActionPolicy = Field(default_factory=ActionPolicy)
    policies: dict[ActionType, ActionPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _redeployment_is_never_automatic(self) -> "HealOpsConfig":
        policy = self.policy_for(ActionType.REQUEST_REDEPLOYMENT)
        if policy.verdict is PolicyVerdict.ALLOW:
            raise ValueError(
                "request_redeployment cannot be fully automatic; "
                "use needs_approval or deny"
            )
        return self

    def policy_for(self, action: ActionType) -> ActionPolicy:
        """Return the policy for `action`, falling back to the default policy."""
        return self.policies.get(action, self.default_policy)