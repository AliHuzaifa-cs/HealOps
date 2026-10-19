"""Validated configuration for HealOps.

Configuration is outside data written by a person, so it is validated at the
boundary with Pydantic. Unknown keys are rejected so a typo such as
`latency_msec` cannot silently disable a threshold.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from healops.capabilities import FRAMEWORK_ACTIONS
from healops.types import ActionType, PolicyVerdict


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ThresholdsConfig(_StrictModel):
    """Limits that turn an observation into an incident. None means 'not monitored'."""

    latency_ms: float | None = Field(default=None, gt=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    drift_score: float | None = Field(default=None, ge=0)
    consecutive_breaches: int = Field(default=3, ge=1, le=20)


class MetricBaseline(_StrictModel):
    """Normal level of a model-quality metric and how far it may move in the bad direction."""

    baseline: float
    max_change: float = Field(gt=0)
    higher_is_better: bool = True

    @property
    def limit(self) -> float:
        """The value beyond which the metric counts as degraded."""
        if self.higher_is_better:
            return self.baseline - self.max_change
        return self.baseline + self.max_change

class VerificationConfig(_StrictModel):
    """How long and how often to watch an application after a recovery action."""

    settle_seconds: int = Field(default=10, ge=0)
    checks: int = Field(default=3, ge=1, le=20)
    interval_seconds: int = Field(default=10, ge=0)
    max_window_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _window_stays_within_the_cap(self) -> "VerificationConfig":
        window = self.settle_seconds + (self.checks - 1) * self.interval_seconds
        if window > self.max_window_seconds:
            raise ValueError(
                f"the verification window ({window}s) is longer than "
                f"max_window_seconds ({self.max_window_seconds}s)"
            )
        return self


class ActionPolicy(_StrictModel):
    """How one kind of recovery action may be used."""

    verdict: PolicyVerdict = PolicyVerdict.NEEDS_APPROVAL
    max_retries: int = Field(default=1, ge=0, le=10)
    cooldown_seconds: int = Field(default=300, ge=0)


class HealOpsConfig(_StrictModel):
    """Top-level configuration. Every default is the safe choice."""

    dry_run: bool = True
    check_interval_seconds: int = Field(default=30, ge=1)
    min_confidence_for_auto: float = Field(default=0.6, ge=0, le=1)
    approval_timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    metric_degradation: dict[str, MetricBaseline] = Field(default_factory=dict)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    default_policy: ActionPolicy = Field(default_factory=ActionPolicy)
    policies: dict[ActionType, ActionPolicy] = Field(default_factory=dict)

    @field_validator("metric_degradation")
    @classmethod
    def _metric_names_are_not_blank(
        cls, value: dict[str, MetricBaseline]
    ) -> dict[str, MetricBaseline]:
        for name in value:
            if not name.strip():
                raise ValueError("metric names must not be blank")
        return value

    @field_validator("policies")
    @classmethod
    def _framework_actions_are_not_configurable(
        cls, value: dict[ActionType, ActionPolicy]
    ) -> dict[ActionType, ActionPolicy]:
        for action in value:
            if action in FRAMEWORK_ACTIONS:
                raise ValueError(
                    f"{action.value} is handled by HealOps itself and cannot be configured"
                )
        return value

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