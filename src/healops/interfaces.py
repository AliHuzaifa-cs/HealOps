"""Capability interfaces an application adapter can implement.

An adapter implements only the capabilities its application really has.
HealOps checks which ones are present instead of calling methods and hoping.

Action methods return a short message describing what happened; it becomes
the `message` of the ActionResult.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class HealthProvider(Protocol):
    """The application can say whether it is healthy."""

    def get_health(self) -> bool: ...


@runtime_checkable
class MetricsProvider(Protocol):
    """The application can report named numbers, e.g. latency_ms or f1_score."""

    def get_metrics(self) -> dict[str, float]: ...


@runtime_checkable
class DriftProvider(Protocol):
    """The application can report a drift score per feature or input."""

    def get_drift_scores(self) -> dict[str, float]: ...


@runtime_checkable
class ComponentRestarter(Protocol):
    """The application can restart one of its components."""

    def restart_component(self, component: str) -> str: ...


@runtime_checkable
class ModelReloader(Protocol):
    """The application can reload its current model artifact."""

    def reload_model(self) -> str: ...


@runtime_checkable
class ModelRollbacker(Protocol):
    """The application can go back to a known-good model version."""

    def rollback_model(self) -> str: ...


@runtime_checkable
class Retrainer(Protocol):
    """The application can start a retraining run (this does NOT deploy the result)."""

    def request_retraining(self) -> str: ...


@runtime_checkable
class Redeployer(Protocol):
    """The application can redeploy (this decides which model or service is live)."""

    def request_redeployment(self) -> str: ...