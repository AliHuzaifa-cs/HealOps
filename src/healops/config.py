"""Validated configuration for HealOps.

Configuration is outside data written by a person, so it is validated at the
boundary with Pydantic. Unknown keys are rejected so a typo such as
`latency_msec` cannot silently disable a threshold.
"""

import re
from typing import Literal

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


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LLMConfig(_StrictModel):
    """Which language model diagnoses incidents. Leave the whole section out for rules only.

    `api_key_env` is the NAME of an environment variable that holds the key. The
    key itself must never be written in a configuration file.
    """

    provider: Literal["ollama", "openai", "anthropic", "openai_compatible"]
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    chat_path: str | None = None
    json_mode: bool = True
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    max_tokens: int = Field(default=800, ge=1, le=8192)
    max_confidence: float = Field(default=0.9, ge=0, le=1)
    max_calls_per_hour: int | None = Field(default=20, ge=1, le=100000)
    use_fallback: bool = True
    fallback_confidence_cap: float = Field(default=0.2, ge=0, le=1)

    @field_validator("model")
    @classmethod
    def _model_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _api_key_env_is_a_name_not_a_key(cls, value: str | None) -> str | None:
        if value is not None and not _ENV_NAME.fullmatch(value):
            raise ValueError(
                "api_key_env must be the NAME of an environment variable "
                "(letters, digits and underscores), never the key itself"
            )
        return value

    @field_validator("chat_path")
    @classmethod
    def _chat_path_is_a_plain_path(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("/") or any(c in value for c in "?# ")
        ):
            raise ValueError("chat_path must start with '/' and be a plain path")
        return value

    @model_validator(mode="after")
    def _provider_specific_rules(self) -> "LLMConfig":
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("provider openai_compatible needs a base_url")
        return self


class AdapterTimeoutsConfig(_StrictModel):
    """How long HealOps waits for the application's adapter, and when it stops trying."""

    read_seconds: float = Field(default=10.0, gt=0, le=600)
    action_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_stuck_calls: int = Field(default=3, ge=1, le=50)


class HealOpsConfig(_StrictModel):
    """Top-level configuration. Every default is the safe choice."""

    dry_run: bool = True
    check_interval_seconds: int = Field(default=30, ge=1)
    min_confidence_for_auto: float = Field(default=0.6, ge=0, le=1)
    approval_timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    audit_log_path: str | None = "healops_audit.jsonl"
    state_db_path: str | None = "healops_state.db"
    state_retention_days: int = Field(default=30, ge=1, le=3650)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    metric_degradation: dict[str, MetricBaseline] = Field(default_factory=dict)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    adapter_timeouts: AdapterTimeoutsConfig = Field(default_factory=AdapterTimeoutsConfig)
    default_policy: ActionPolicy = Field(default_factory=ActionPolicy)
    policies: dict[ActionType, ActionPolicy] = Field(default_factory=dict)
    llm: LLMConfig | None = None

    @field_validator("audit_log_path", "state_db_path")
    @classmethod
    def _path_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a path must not be blank (use null to keep it in memory)")
        return value

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