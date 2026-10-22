"""Reads an application's current state and turns it into observations.

This is the "Monitor" step. It only reads: it never changes the application.
A provider that fails does not stop the others. A failing health check counts as
unhealthy, because "cannot find out" is not "healthy". A failing metrics or drift
read is reported as an error instead, because inventing a number would be worse.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from healops.events import Observation
from healops.interfaces import DriftProvider, HealthProvider, MetricsProvider
from healops.registry import RegisteredApplication
from healops.types import ObservationKind

HEALTH_NAME = "health"


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """What one collection pass saw, and what it could not read."""

    observations: tuple[Observation, ...]
    errors: tuple[str, ...] = ()


def collect(
    application: RegisteredApplication,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CollectionResult:
    """Read health, metrics and drift scores, whichever the application provides."""
    name, adapter, caps = application.name, application.adapter, application.capabilities
    now = clock()
    observations: list[Observation] = []
    errors: list[str] = []

    if caps.has(HealthProvider):
        try:
            healthy = adapter.get_health()  # type: ignore[attr-defined]
            details: dict[str, str] = {}
        except Exception as error:  # noqa: BLE001 - a failing probe must not stop monitoring
            healthy = False
            details = {"error": type(error).__name__}
            errors.append(f"health check failed ({type(error).__name__})")
        observations.append(
            Observation(name, ObservationKind.HEALTH, HEALTH_NAME, bool(healthy), now, details)
        )

    for capability, kind, method in (
        (MetricsProvider, ObservationKind.METRIC, "get_metrics"),
        (DriftProvider, ObservationKind.DRIFT, "get_drift_scores"),
    ):
        if not caps.has(capability):
            continue
        try:
            readings = getattr(adapter, method)()
        except Exception as error:  # noqa: BLE001 - one failing provider must not stop the rest
            errors.append(f"{method} failed ({type(error).__name__})")
            continue
        for key, value in readings.items():
            try:
                observations.append(Observation(name, kind, key, _number(value), now))
            except (TypeError, ValueError) as problem:
                errors.append(f"{method}: ignored {key!r} ({type(problem).__name__})")

    return CollectionResult(tuple(observations), tuple(errors))


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("readings must be numbers")
    if not math.isfinite(value):
        raise ValueError("readings must be finite")
    return float(value)