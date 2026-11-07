"""Threshold detectors: health failure, high latency, high error rate and data drift.

Observation naming convention (what the monitoring layer must produce):
  * health:  kind HEALTH, any name, value True/False
  * metrics: kind METRIC, name is the metric key (e.g. "latency_ms", "error_rate")
  * drift:   kind DRIFT, name is the feature or input, value is its drift score
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from healops.config import MetricBaseline, ThresholdsConfig
from healops.detection.base import BreachTracker
from healops.events import Incident, Observation
from healops.types import IncidentType, ObservationKind, Severity


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """One condition that counts as a breach.

    A HEALTH rule has no limit: a False value is the breach. Every other rule
    needs a limit: a numeric value above it is the breach, or below it when
    `below` is True (for metrics where a higher value is better).
    """

    kind: ObservationKind
    incident_type: IncidentType
    severity: Severity
    observation_name: str | None = None  # None means any name of this kind
    limit: float | None = None
    below: bool = False

    def __post_init__(self) -> None:
        if self.kind is ObservationKind.HEALTH and self.limit is not None:
            raise ValueError("a health rule must not have a limit")
        if self.kind is ObservationKind.HEALTH and self.below:
            raise ValueError("a health rule cannot use 'below'")
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
        if self.below:
            return value < self.limit
        return value > self.limit


class ThresholdDetector:
    """Creates an incident when a rule is breached several checks in a row.

    Breaches are counted per signal (application, incident type, observation name), but one
    underlying problem is one incident: all signals of the same application and incident type are
    grouped. When several of them cross the limit (for example the PSI and the KS distance of the
    same input, which move together), a single incident carries all of them as evidence. While that
    incident is open, further signals of the same group do not open another one.
    """

    def __init__(self, rules: Sequence[ThresholdRule], consecutive_breaches: int) -> None:
        self._rules = tuple(rules)
        self._required = consecutive_breaches
        self._tracker = BreachTracker(consecutive_breaches)
        self._runs: dict[str, list[Observation]] = {}
        self._open: dict[str, str] = {}  # incident id -> group key
        self._open_groups: set[str] = set()
        self._signals: dict[str, set[str]] = {}  # group key -> signal keys seen
        self._clear: dict[str, int] = {}  # signal key -> readings in a row that were normal

    @staticmethod
    def _group(app_name: str, rule: ThresholdRule) -> str:
        return f"{app_name}|{rule.incident_type.value}"

    def evaluate(self, observations: Sequence[Observation]) -> list[Incident]:
        fired: dict[str, tuple[ThresholdRule, Observation]] = {}
        for observation in observations:
            for rule in self._rules:
                if not rule.matches(observation):
                    continue
                group = self._group(observation.app_name, rule)
                key = f"{group}|{observation.name}"
                self._signals.setdefault(group, set()).add(key)
                breached = rule.is_breached(observation)
                self._clear[key] = 0 if breached else self._clear.get(key, 0) + 1
                run = self._runs.setdefault(key, [])
                if breached:
                    run.append(observation)
                    if len(run) > self._required:
                        del run[: len(run) - self._required]
                else:
                    run.clear()
                if self._tracker.record(key, breached) and group not in fired:
                    fired[group] = (rule, observation)

        incidents: list[Incident] = []
        for group, (rule, observation) in fired.items():
            if group in self._open_groups:
                continue  # the same problem is already being handled
            evidence: list[Observation] = []
            names: list[str] = []
            for key in sorted(self._signals[group]):
                if self._tracker.count(key) >= self._required and self._runs.get(key):
                    evidence.extend(self._runs[key])
                    names.append(key.rsplit("|", 1)[1])
            incident = Incident(
                app_name=observation.app_name,
                incident_type=rule.incident_type,
                severity=rule.severity,
                summary=self._summary(rule, observation, names),
                evidence=tuple(evidence),
            )
            self._open[incident.incident_id] = group
            self._open_groups.add(group)
            incidents.append(incident)
        return incidents

    def watched(self) -> frozenset[tuple[ObservationKind, str | None]]:
        """What the rules look at: (kind, name), with name None for 'any of this kind'."""
        return frozenset((rule.kind, rule.observation_name) for rule in self._rules)

    def hold(self, incident: Incident) -> None:
        """Remember that `incident` is still open (after a restart), so it is not raised twice."""
        for rule in self._rules:
            if rule.incident_type is not incident.incident_type:
                continue
            group = self._group(incident.app_name, rule)
            self._open[incident.incident_id] = group
            self._open_groups.add(group)
            for name in {o.name for o in incident.evidence}:
                key = f"{group}|{name}"
                self._signals.setdefault(group, set()).add(key)
                self._tracker.hold(key)
            return

    def is_clear(self, incident_id: str, checks: int) -> bool:
        """True when every signal behind an open incident has been normal for `checks` readings
        in a row. An incident this detector does not know is never clear."""
        group = self._open.get(incident_id)
        if group is None or checks < 1:
            return False
        keys = self._signals.get(group, ())
        return bool(keys) and all(self._clear.get(k, 0) >= checks for k in keys)

    def release(self, incident_id: str) -> None:
        """Call when an incident is closed, so the same problem can be detected again."""
        group = self._open.pop(incident_id, None)
        if group is None:
            return
        self._open_groups.discard(group)
        for key in self._signals.get(group, ()):
            self._tracker.reset(key)
            self._runs.pop(key, None)

    def _summary(self, rule: ThresholdRule, observation: Observation, names: list[str]) -> str:
        if rule.limit is None:
            return f"{observation.name} check failed {self._required} times in a row"
        side = "below" if rule.below else "above"
        extra = f" (also {', '.join(n for n in names if n != observation.name)})" if len(names) > 1 else ""
        return (
            f"{observation.name} was {side} {rule.limit:g} for {self._required} "
            f"checks in a row (latest {float(observation.value):g}){extra}"
        )


def build_threshold_rules(
    thresholds: ThresholdsConfig,
    metric_degradation: Mapping[str, MetricBaseline] | None = None,
) -> list[ThresholdRule]:
    """Turn config into rules. A threshold left as None is not monitored.

    `metric_degradation` adds one rule per model-quality metric. A metric that
    is not reported in a check is simply skipped: silence is not a breach.
    """
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
    for name, spec in (metric_degradation or {}).items():
        # A model giving worse predictions is serious even when the service is up.
        rules.append(
            ThresholdRule(
                kind=ObservationKind.METRIC,
                incident_type=IncidentType.PERFORMANCE_DEGRADATION,
                severity=Severity.HIGH,
                observation_name=name,
                limit=spec.limit,
                below=spec.higher_is_better,
            )
        )
    return rules


def build_threshold_detector(
    thresholds: ThresholdsConfig,
    metric_degradation: Mapping[str, MetricBaseline] | None = None,
) -> ThresholdDetector:
    """Build a detector from config."""
    rules = build_threshold_rules(thresholds, metric_degradation)
    return ThresholdDetector(rules, thresholds.consecutive_breaches)