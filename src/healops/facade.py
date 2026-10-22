"""The public entry point: one object that wires every HealOps part together.

    healops = HealOps.from_config("selfhealing.yaml")
    healops.register("my-app", MyAdapter(), component="model-server")
    healops.run()

Safe by default: dry-run is on (unless the config says otherwise), actions need
the policy engine's permission, and every step goes to the audit log.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from healops.agents.base import DiagnosisAgent
from healops.agents.factory import build_agent
from healops.audit import AuditLog, JsonlAuditLog, MemoryAuditLog
from healops.capabilities import Capabilities
from healops.config import HealOpsConfig
from healops.detection.thresholds import build_threshold_detector
from healops.events import Incident, _now
from healops.healing.approval import ApprovalQueue, PendingApproval
from healops.healing.history import ActionHistory
from healops.healing.notify import ConsoleNotifier, Notifier, SafeNotifier
from healops.healing.orchestrator import HandleResult, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.interfaces import ComponentRestarter
from healops.llm.http import Transport
from healops.loader import load_config
from healops.loop import LoopStats, MonitoringLoop, TickReport
from healops.registry import ApplicationRegistry, RegisteredApplication
from healops.verification.base import Verifier
from healops.verification.postconditions import PostconditionVerifier

log = logging.getLogger(__name__)


class HealOps:
    """Monitors registered applications and heals them within the configured rules."""

    def __init__(
        self,
        config: HealOpsConfig | Mapping[str, object] | str | Path,
        *,
        agent: DiagnosisAgent | None = None,
        notifiers: Sequence[Notifier] | None = None,
        audit: AuditLog | None = None,
        verifier: Verifier | None = None,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], None] | None = None,
        on_tick: Callable[[TickReport], None] | None = None,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(config, (str, Path)):
            config = load_config(config)
        elif not isinstance(config, HealOpsConfig):
            config = HealOpsConfig.model_validate(dict(config))
        self.config = config

        self._registry = ApplicationRegistry()
        self._components: dict[str, str] = {}
        self._detector = build_threshold_detector(config.thresholds, config.metric_degradation)
        self._approvals = ApprovalQueue(config.approval_timeout_seconds, clock=clock)
        self._audit = audit if audit is not None else _default_audit(config)
        self._agent = (
            agent
            if agent is not None
            else build_agent(config, component=self._components, transport=transport, environ=environ)
        )
        verifier_ = verifier
        if verifier_ is None:
            verifier_ = (
                PostconditionVerifier(config, sleep=sleep)
                if sleep is not None
                else PostconditionVerifier(config)
            )
        orchestrator = Orchestrator(
            registry=self._registry,
            agent=self._agent,
            policy=PolicyEngine(config, history=ActionHistory(), clock=clock),
            approvals=self._approvals,
            notifier=SafeNotifier(list(notifiers) if notifiers is not None else [ConsoleNotifier()]),
            dry_run=config.dry_run,
            clock=clock,
            verifier=verifier_,
            on_resolved=self._detector.release,
            audit=self._audit,
        )
        self._loop = MonitoringLoop(
            self._registry,
            self._detector,
            orchestrator,
            interval_seconds=config.check_interval_seconds,
            on_tick=on_tick,
        )
        if config.dry_run:
            log.info("HealOps is in dry-run mode: actions are logged but not executed")

    @classmethod
    def from_config(cls, path: str | Path, **options: object) -> "HealOps":
        return cls(load_config(path), **options)  # type: ignore[arg-type]

    # ----- setup -----

    def register(
        self, name: str, adapter: object, component: str | None = None
    ) -> RegisteredApplication:
        """Put an application under HealOps' care.

        `component` is the name passed to the adapter's restart method. It is
        required when the adapter can restart components.
        """
        if component is not None and not component.strip():
            raise ValueError("component must be a non-empty string")
        if component is None and Capabilities.discover(adapter).has(ComponentRestarter):
            raise ValueError(
                f"{name!r} can restart components, so register it with component='<name>'"
            )
        application = self._registry.register(name, adapter)
        if component is not None:
            self._components[name] = component
        return application

    # ----- running -----

    def run(self, max_ticks: int | None = None) -> LoopStats:
        """Monitor until `stop()` is called (or `max_ticks` checks have run)."""
        self._require_apps()
        return self._loop.run(max_ticks=max_ticks)

    def tick(self) -> TickReport:
        """One check-detect-heal cycle."""
        self._require_apps()
        return self._loop.tick()

    def stop(self) -> None:
        self._loop.stop()

    @property
    def stats(self) -> LoopStats:
        return self._loop.stats

    # ----- people -----

    def pending_approvals(self) -> tuple[PendingApproval, ...]:
        return self._approvals.pending()

    def incidents(self) -> dict[str, Incident]:
        """Incidents that are escalated or waiting for a person, by incident id."""
        return {key: result.incident for key, result in self._loop.open_incidents.items()}

    def approve(self, incident_id: str, proposal_id: str, approver: str) -> HandleResult:
        return self._loop.approve(incident_id, proposal_id, approver)

    def reject(
        self, incident_id: str, proposal_id: str, approver: str, reason: str | None = None
    ) -> HandleResult:
        return self._loop.reject(incident_id, proposal_id, approver, reason)

    def acknowledge(self, incident_id: str) -> None:
        self._loop.acknowledge(incident_id)

    def _require_apps(self) -> None:
        if len(self._registry) == 0:
            raise ValueError("no application is registered; call register() first")


def _default_audit(config: HealOpsConfig) -> AuditLog:
    if config.audit_log_path is None:
        return MemoryAuditLog()
    return JsonlAuditLog(config.audit_log_path)


SelfHealingAgent = HealOps