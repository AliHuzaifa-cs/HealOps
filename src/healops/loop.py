"""The monitoring loop: check every application on a schedule and heal what breaks.

One tick does, for every registered application:

  1. collect its readings (health, metrics, drift),
  2. let the detector decide whether an incident has started,
  3. take each new incident as far as it can go without a person
     (diagnose, policy, action, verification),

and then escalates every approval that waited too long.

Design rules:

  * One thread, one incident at a time. Simple and predictable.
  * A failure in one application or one incident never stops the loop; it is
    reported in the tick report and logged without secrets.
  * An incident that is escalated or waiting for a person stays "open" in the
    detector, so the same problem does not raise the same alert every tick.
    Call `acknowledge()` once a person has dealt with it.
  * The loop runs actions only through the orchestrator, so every safety check
    (policy, approval, dry-run, audit) applies exactly as before.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from healops.collector import collect
from healops.events import Incident, Observation
from healops.healing.orchestrator import HandleResult, HandleStatus, Orchestrator
from healops.registry import ApplicationRegistry

log = logging.getLogger("healops.loop")

MAX_STEPS = 20
RECENT_PER_APP = 50

# Statuses where the incident is finished, or waits for a person.
_AT_REST = (HandleStatus.RESOLVED, HandleStatus.ESCALATED, HandleStatus.PARKED)


class LoopDetector(Protocol):
    def evaluate(self, observations: Sequence[Observation]) -> list[Incident]: ...

    def release(self, incident_id: str) -> None: ...


def drive_to_rest(orchestrator: Orchestrator, result: HandleResult) -> HandleResult:
    """Execute and verify until the incident is resolved, escalated or waiting for a person."""
    for _ in range(MAX_STEPS):
        if result.status is HandleStatus.READY:
            result = orchestrator.execute(result)
        elif result.status is HandleStatus.EXECUTED:
            result = orchestrator.verify(result)
        else:
            return result
    raise RuntimeError("the incident did not settle within the step limit")


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick saw and did."""

    tick: int
    observations: int = 0
    detected: tuple[Incident, ...] = ()
    results: tuple[HandleResult, ...] = ()
    expired: tuple[HandleResult, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class LoopStats:
    ticks: int = 0
    incidents: int = 0
    resolved: int = 0
    escalated: int = 0
    parked: int = 0
    errors: int = 0


class MonitoringLoop:
    """Runs the check-detect-heal cycle."""

    def __init__(
        self,
        registry: ApplicationRegistry,
        detector: LoopDetector,
        orchestrator: Orchestrator,
        interval_seconds: float = 30.0,
        on_tick: Callable[[TickReport], None] | None = None,
        wait: Callable[[float], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._registry = registry
        self._detector = detector
        self._orchestrator = orchestrator
        self._interval = interval_seconds
        self._on_tick = on_tick
        self._stop = threading.Event()
        # `wait(seconds)` returns True when the loop should stop.
        self._wait = wait if wait is not None else self._stop.wait
        self._monotonic = monotonic
        self._recent: dict[str, deque[Observation]] = {}
        self._open: dict[str, HandleResult] = {}
        self.stats = LoopStats()

    # ----- control -----

    def stop(self) -> None:
        """Ask the loop to stop after the current tick."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self, max_ticks: int | None = None) -> LoopStats:
        """Tick until stopped (or until `max_ticks`, which is handy in tests and demos)."""
        if max_ticks is not None and max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        while not self._stop.is_set():
            started = self._monotonic()
            self.tick()
            if max_ticks is not None and self.stats.ticks >= max_ticks:
                break
            pause = max(0.0, self._interval - (self._monotonic() - started))
            if self._wait(pause):
                break
        return self.stats

    # ----- one tick -----

    def tick(self) -> TickReport:
        self.stats.ticks += 1
        errors: list[str] = []
        observed = 0
        detected: list[Incident] = []
        results: list[HandleResult] = []

        for name in self._registry.names():
            application = self._registry.get(name)
            try:
                collected = collect(application)
            except Exception as error:  # noqa: BLE001 - one application must not stop the others
                errors.append(self._problem(f"collecting {name}", error))
                continue
            errors.extend(f"{name}: {message}" for message in collected.errors)
            observed += len(collected.observations)
            recent = self._recent.setdefault(name, deque(maxlen=RECENT_PER_APP))
            recent.extend(collected.observations)

            try:
                new = self._detector.evaluate(collected.observations)
            except Exception as error:  # noqa: BLE001 - a broken detector must not stop the loop
                errors.append(self._problem(f"detecting for {name}", error))
                continue
            for incident in new:
                detected.append(incident)
                self.stats.incidents += 1
                outcome = self._handle(incident, tuple(recent), errors)
                if outcome is not None:
                    results.append(outcome)

        expired = self._expire(errors)
        self.stats.errors += len(errors)
        report = TickReport(
            self.stats.ticks, observed, tuple(detected), tuple(results), tuple(expired), tuple(errors)
        )
        if self._on_tick is not None:
            try:
                self._on_tick(report)
            except Exception as error:  # noqa: BLE001 - a broken callback must not stop the loop
                log.warning("on_tick callback failed (%s)", type(error).__name__)
        return report

    # ----- people -----

    @property
    def open_incidents(self) -> dict[str, HandleResult]:
        """Incidents that are escalated or waiting for a person, by incident id."""
        return dict(self._open)

    def approve(self, incident_id: str, proposal_id: str, approver: str) -> HandleResult:
        """A person approved a waiting action; carry it through to the end."""
        ready = self._orchestrator.approve(incident_id, proposal_id, approver)
        return self._settle(ready)

    def reject(
        self, incident_id: str, proposal_id: str, approver: str, reason: str | None = None
    ) -> HandleResult:
        """A person refused a waiting action; the incident is escalated."""
        return self._settle(self._orchestrator.reject(incident_id, proposal_id, approver, reason))

    def acknowledge(self, incident_id: str) -> None:
        """A person dealt with an escalated incident; the same problem may be detected again."""
        self._open.pop(incident_id, None)
        self._detector.release(incident_id)

    # ----- internals -----

    def _handle(
        self, incident: Incident, recent: tuple[Observation, ...], errors: list[str]
    ) -> HandleResult | None:
        try:
            result = drive_to_rest(self._orchestrator, self._orchestrator.handle(incident, recent))
        except Exception as error:  # noqa: BLE001 - an unexpected failure must not stop the loop
            errors.append(self._problem(f"handling an incident on {incident.app_name}", error))
            self._detector.release(incident.incident_id)  # let it be detected and tried again
            return None
        return self._settle(result)

    def _settle(self, result: HandleResult) -> HandleResult:
        """Finish a result and remember whether the incident still needs a person."""
        if result.status not in _AT_REST:
            result = drive_to_rest(self._orchestrator, result)
        incident_id = result.incident.incident_id
        if result.status is HandleStatus.RESOLVED:
            self.stats.resolved += 1
            self._open.pop(incident_id, None)
            self._detector.release(incident_id)
        else:
            if result.status is HandleStatus.PARKED:
                self.stats.parked += 1
            else:
                self.stats.escalated += 1
            self._open[incident_id] = result
        return result

    def _expire(self, errors: list[str]) -> list[HandleResult]:
        try:
            expired = self._orchestrator.expire_overdue_approvals()
        except Exception as error:  # noqa: BLE001 - must not stop the loop
            errors.append(self._problem("expiring approvals", error))
            return []
        for result in expired:
            self.stats.escalated += 1
            self._open[result.incident.incident_id] = result
        return expired

    @staticmethod
    def _problem(what: str, error: Exception) -> str:
        message = f"{what} failed ({type(error).__name__})"
        log.warning(message)
        return message