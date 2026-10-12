"""Threshold detectors: health failure, high latency, high error rate and data drift.

Observation naming convention (what the monitoring layer must produce):
  * health:  kind HEALTH, any name, value True/False
  * metrics: kind METRIC, name is the metric key (e.g. "latency_ms", "error_rate")
  * drift:   kind DRIFT, name is the feature or input, value is its drift score
"""

from collections.abc import Sequence
from dataclasses import dataclass

from healops.config import ThresholdsConfig
from healops.detection.base import BreachTracker
from healops.events import Incident, Observation
from healops.types import IncidentType, ObservationKind, Severity


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """One condition that counts as a breach.

    A HEALTH rule has no limit: a False value is the breach. Every other rule
    needs a limit: a numeric value above it is the breach.
    """

    kind: ObservationKind
    incident_type: IncidentType
    severity: Severity
    observation_name: str | None = None  # None means any name of this kind
    limit: float | None = None

    def __post_init__(self) -> None:
        if self.kind is ObservationKind.HEALTH and self.limit is not None:
            raise ValueError("a health rule must not have a limit")
        if self.kind is not ObservationKind.HEALTH and self.limit is None:
            raise ValueError("a metric or drift rule needs a limit")

    def matches(self, observation: Observation) -> bool:
        if observation.kind is not self.kind:
            return False
        return self.observation_name is None or observation.name == self.observation_name

    def is_breached(self, observation: Observation) -> bool:
        value = observation.value
        if self.limit is None:
            return value is False
        if isinstance(value, bool):
            return False
        return value > self.limit


class ThresholdDetector:
    """Creates an incident when a rule is breached several checks in a row."""

    def __init__(self, rules: Sequence[ThresholdRule], consecutive_breaches: int) -> None:
        self._rules = tuple(rules)
        self._required = consecutive_breaches
        self._tracker = BreachTracker(consecutive_breaches)
        self._runs: dict[str, list[Observation]] = {}
        self._open: dict[str, str] = {}

    def evaluate(self, observations: Sequence[Observation]) -> list[Incident]:
        incidents: list[Incident] = []
        for observation in observations:
            for rule in self._rules:
                if not rule.matches(observation):
                    continue
                key = f"{observation.app_name}|{rule.incident_type.value}|{observation.name}"
                breached = rule.is_breached(observation)
                run = self._runs.setdefault(key, [])
                if breached:
                    run.append(observation)
                    if len(run) > self._required:
                        del run[: len(run) - self._required]
                else:
                    run.clear()
                if self._tracker.record(key, breached):
                    incident = Incident(
                        app_name=observation.app_name,
                        incident_type=rule.incident_type,
                        severity=rule.severity,
                        summary=self._summary(rule, observation),
                        evidence=tuple(run),
                    )
                    self._open[incident.incident_id] = key
                    incidents.append(incident)
        return incidents

    def release(self, incident_id: str) -> None:
        """Call when an incident is closed, so the same problem can be detected again."""
        key = self._open.pop(incident_id, None)
        if key is not None:
            self._tracker.reset(key)
            self._runs.pop(key, None)

    def _summary(self, rule: ThresholdRule, observation: Observation) -> str:
        if rule.limit is None:
            return f"{observation.name} check failed {self._required} times in a row"
        return (
            f"{observation.name} was above {rule.limit:g} for {self._required} "
            f"checks in a row (latest {float(observation.value):g})"
        )


def build_threshold_detector(thresholds: ThresholdsConfig) -> ThresholdDetector:
    """Build a detector from config. A threshold left as None is not monitored."""
    rules = [
        ThresholdRule(
            kind=ObservationKind.HEALTH,
            incident_type=IncidentType.HEALTH_FAILURE,
            severity=Severity.CRITICAL,
        )
    ]
    if thresholds.latency_ms is not None:
        rules.append(
            ThresholdRule(
                kind=ObservationKind.METRIC,
                incident_type=IncidentType.HIGH_LATENCY,
                severity=Severity.MEDIUM,
                observation_name="latency_ms",
                limit=thresholds.latency_ms,
            )
        )
    if thresholds.error_rate is not None:
        rules.append(
            ThresholdRule(
                kind=ObservationKind.METRIC,
                incident_type=IncidentType.HIGH_ERROR_RATE,
                severity=Severity.HIGH,
                observation_name="error_rate",
                limit=thresholds.error_rate,
            )
        )
    if thresholds.drift_score is not None:
        # Drift alone is not a failure, so its severity stays low.
        rules.append(
            ThresholdRule(
                kind=ObservationKind.DRIFT,
                incident_type=IncidentType.DATA_DRIFT,
                severity=Severity.LOW,
                limit=thresholds.drift_score,
            )
        )
    return ThresholdDetector(rules, thresholds.consecutive_breaches)