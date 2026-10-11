"""What an application adapter can do, discovered from the adapter itself."""

from dataclasses import dataclass

from healops.interfaces import (
    ComponentRestarter,
    DriftProvider,
    HealthProvider,
    MetricsProvider,
    ModelReloader,
    ModelRollbacker,
    Redeployer,
    Retrainer,
)
from healops.types import ActionType

ALL_CAPABILITIES: tuple[type, ...] = (
    HealthProvider,
    MetricsProvider,
    DriftProvider,
    ComponentRestarter,
    ModelReloader,
    ModelRollbacker,
    Retrainer,
    Redeployer,
)

# Each action that changes the application needs exactly one capability.
ACTION_REQUIREMENTS: dict[ActionType, type] = {
    ActionType.RESTART_COMPONENT: ComponentRestarter,
    ActionType.RELOAD_MODEL: ModelReloader,
    ActionType.ROLLBACK_MODEL: ModelRollbacker,
    ActionType.REQUEST_RETRAINING: Retrainer,
    ActionType.REQUEST_REDEPLOYMENT: Redeployer,
}

# Actions HealOps handles itself; no adapter capability is needed.
FRAMEWORK_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE, ActionType.NO_ACTION}
)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The set of capability interfaces an adapter implements."""

    provided: frozenset[type]

    def __post_init__(self) -> None:
        unknown = self.provided - set(ALL_CAPABILITIES)
        if unknown:
            raise ValueError(f"unknown capabilities: {sorted(c.__name__ for c in unknown)}")

    @classmethod
    def discover(cls, adapter: object) -> "Capabilities":
        """Inspect `adapter` and record which capability interfaces it implements."""
        provided = frozenset(cap for cap in ALL_CAPABILITIES if isinstance(adapter, cap))
        if not provided:
            raise ValueError(
                f"{type(adapter).__name__} implements no HealOps capability; "
                "check that its method names match the interfaces"
            )
        return cls(provided=provided)

    def has(self, capability: type) -> bool:
        return capability in self.provided

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(cap.__name__ for cap in self.provided))

    def supported_actions(self) -> frozenset[ActionType]:
        """Actions this application can actually carry out."""
        from_adapter = {
            action
            for action, required in ACTION_REQUIREMENTS.items()
            if required in self.provided
        }
        return frozenset(from_adapter) | FRAMEWORK_ACTIONS