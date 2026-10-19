"""A generic verifier: the application must stay healthy and inside its limits.

After an action it waits a moment, then checks several times. Recovery is the
opposite of detection: the same rules that raised the incident must stop being
breached, in every check. One good reading is not enough, and a reading that is
missing is never counted as good.
"""

import time
from collections import Counter
from collections.abc import Callable
from typing import cast

from healops.config import HealOpsConfig
from healops.detection.thresholds import ThresholdRule, build_threshold_rules
from healops.events import Incident, Observation
from healops.interfaces import DriftProvider, HealthProvider, MetricsProvider
from healops.outcomes import ActionResult, VerificationResult
from healops.registry import RegisteredApplication
from healops.types import IncidentType, ObservationKind, VerificationOutcome

Sleep = Callable[[float], None]

_GOOD, _BAD, _UNKNOWN = "good", "bad", "unknown"


class PostconditionVerifier:
    """Checks health plus the rules that belong to the incident's type."""

    def __init__(self, config: HealOpsConfig, sleep: Sleep = time.sleep) -> None:
        self._settings = config.verification
        self._rules = build_threshold_rules(config.thresholds, config.metric_degradation)
        self._sleep = sleep

    def verify(
        self,
        application: RegisteredApplication,
        incident: Incident,
        result: ActionResult,
    ) -> VerificationResult:
        signals = self._signals(application, incident)
        has_specific = any(rule.kind is not ObservationKind.HEALTH for rule, _ in signals)
        needs_specific = incident.incident_type is not IncidentType.HEALTH_FAILURE
        if not signals or (needs_specific and not has_specific):
            note = f"no recovery check is defined for {incident.incident_type.value}"
            return self._result(result, VerificationOutcome.INCONCLUSIVE, [], [note])

        settings = self._settings
        statuses: list[str] = []
        passes: Counter[str] = Counter()
        breaches: Counter[str] = Counter()
        missing: Counter[str] = Counter()
        latest: dict[str, object] = {}

        self._sleep(settings.settle_seconds)
        for index in range(settings.checks):
            if index > 0:
                self._sleep(settings.interval_seconds)
            seen = self._observe(application)
            check_status = _GOOD
            for rule, name in signals:
                observation = seen.get((rule.kind, name))
                if observation is None:
                    missing[name] += 1
                    if check_status == _GOOD:
                        check_status = _UNKNOWN
                elif rule.is_breached(observation):
                    breaches[name] += 1
                    latest[name] = observation.value
                    check_status = _BAD
                else:
                    passes[name] += 1
            statuses.append(check_status)

        total = settings.checks
        passed = [f"{label}: ok in {count}/{total} checks" for label, count in passes.items()]
        failed = [
            f"{label}: breached in {count}/{total} checks (latest {latest[label]})"
            for label, count in breaches.items()
        ]
        failed += [
            f"{label}: not observed in {count}/{total} checks" for label, count in missing.items()
        ]

        good, bad = statuses.count(_GOOD), statuses.count(_BAD)
        if bad == 0 and good == total:
            outcome = VerificationOutcome.VERIFIED
        elif bad == 0:
            outcome = VerificationOutcome.INCONCLUSIVE
        elif good == 0:
            outcome = VerificationOutcome.FAILED
        else:
            outcome = VerificationOutcome.PARTIAL
        return self._result(result, outcome, passed, failed)

    def _signals(
        self, application: RegisteredApplication, incident: Incident
    ) -> list[tuple[ThresholdRule, str]]:
        """What must hold: health, plus the rules for this incident's type."""
        signals: list[tuple[ThresholdRule, str]] = []
        names = {observation.name for observation in incident.evidence}
        for rule in self._rules:
            if rule.kind is ObservationKind.HEALTH:
                if application.capabilities.has(HealthProvider):
                    signals.append((rule, "health"))
            elif rule.incident_type is incident.incident_type:
                for name in sorted(names):
                    if rule.observation_name in (None, name):
                        signals.append((rule, name))
        return signals

    @staticmethod
    def _observe(
        application: RegisteredApplication,
    ) -> dict[tuple[ObservationKind, str], Observation]:
        """Take one fresh reading. A callback that fails gives no reading (health: unhealthy)."""
        adapter = application.adapter
        capabilities = application.capabilities
        app_name = application.name
        seen: dict[tuple[ObservationKind, str], Observation] = {}

        if capabilities.has(HealthProvider):
            try:
                healthy = bool(cast(HealthProvider, adapter).get_health())
            except Exception:  # noqa: BLE001 - a health probe that fails means not healthy
                healthy = False
            seen[(ObservationKind.HEALTH, "health")] = Observation(
                app_name, ObservationKind.HEALTH, "health", healthy
            )

        sources = (
            (MetricsProvider, ObservationKind.METRIC, "get_metrics"),
            (DriftProvider, ObservationKind.DRIFT, "get_drift_scores"),
        )
        for protocol, kind, method in sources:
            if not capabilities.has(protocol):
                continue
            for name, value in PostconditionVerifier._read(adapter, method).items():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    seen[(kind, str(name))] = Observation(app_name, kind, str(name), float(value))
        return seen

    @staticmethod
    def _read(adapter: object, method: str) -> dict:
        """Call a reading method. If it fails there is simply no reading."""
        try:
            return dict(getattr(adapter, method)())
        except Exception:  # noqa: BLE001 - no reading is treated as 'not observed'
            return {}

    @staticmethod
    def _result(
        action: ActionResult,
        outcome: VerificationOutcome,
        passed: list[str],
        failed: list[str],
    ) -> VerificationResult:
        return VerificationResult(
            execution_id=action.execution_id,
            outcome=outcome,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )