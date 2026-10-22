"""A ready-made HealOps system around the ToyApp, for demos and scenario tests.

It wires the real parts together (collector, detector, agent, policy, approvals,
orchestrator, verifier) and adds two small drivers that stand in for the
monitoring loop that does not exist yet: `detect` and `heal`.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from healops.agents.base import DiagnosisAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.audit import MemoryAuditLog
from healops.collector import collect
from healops.config import HealOpsConfig
from healops.detection.thresholds import ThresholdDetector, build_threshold_detector
from healops.events import Incident
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleResult, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.loop import drive_to_rest
from healops.registry import ApplicationRegistry, RegisteredApplication
from healops.testing.toy_app import COMPONENT, ToyApp
from healops.verification.postconditions import PostconditionVerifier

APP_NAME = "toy-app"

DEFAULT_CONFIG: dict[str, Any] = {
    "min_confidence_for_auto": 0.3,
    "thresholds": {
        "latency_ms": 500,
        "error_rate": 0.05,
        "drift_score": 0.3,
        "consecutive_breaches": 2,
    },
    "metric_degradation": {"f1_score": {"baseline": 0.90, "max_change": 0.10}},
    "verification": {
        "settle_seconds": 0,
        "checks": 2,
        "interval_seconds": 0,
        "max_window_seconds": 60,
    },
    "policies": {
        "restart_component": {"verdict": "allow", "cooldown_seconds": 0},
        "reload_model": {"verdict": "allow", "cooldown_seconds": 0},
        "rollback_model": {"verdict": "allow", "cooldown_seconds": 0},
    },
}


@dataclass(slots=True)
class ToySystem:
    """Everything needed to run one fault from detection to the end of recovery."""

    app: ToyApp
    application: RegisteredApplication
    detector: ThresholdDetector
    orchestrator: Orchestrator
    alerts: RecordingNotifier
    audit: MemoryAuditLog
    registry: ApplicationRegistry


def make_toy_system(
    config: HealOpsConfig | Mapping[str, Any] | None = None,
    agent: DiagnosisAgent | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> ToySystem:
    """Build a toy application and a full HealOps system around it."""
    if config is None:
        config = HealOpsConfig.model_validate(DEFAULT_CONFIG)
    elif not isinstance(config, HealOpsConfig):
        config = HealOpsConfig.model_validate(dict(config))

    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = clock if clock is not None else (lambda: now)
    app = ToyApp()
    registry = ApplicationRegistry()
    application = registry.register(APP_NAME, app)
    detector = build_threshold_detector(config.thresholds, config.metric_degradation)
    alerts = RecordingNotifier()
    audit = MemoryAuditLog()
    history = ActionHistory()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent if agent is not None else MockDiagnosisAgent(component=COMPONENT),
        policy=PolicyEngine(config, history=history, clock=clock),
        approvals=ApprovalQueue(config.approval_timeout_seconds, clock=clock),
        notifier=SafeNotifier([alerts]),
        dry_run=dry_run,
        clock=clock,
        verifier=PostconditionVerifier(config, sleep=lambda _seconds: None),
        on_resolved=detector.release,
        audit=audit,
    )
    return ToySystem(app, application, detector, orchestrator, alerts, audit, registry)


def detect(system: ToySystem, max_checks: int = 10) -> list[Incident]:
    """Check the app repeatedly until something is detected, or give up.

    Returns the incidents found in the first check that finds any, else [].
    """
    for _ in range(max_checks):
        observations = collect(system.application).observations
        incidents = system.detector.evaluate(observations)
        if incidents:
            return incidents
    return []


def heal(system: ToySystem, incident: Incident) -> HandleResult:
    """Drive one incident until it is resolved, escalated or waiting for a person."""
    return drive_to_rest(system.orchestrator, system.orchestrator.handle(incident))