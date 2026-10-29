"""Sample data for `healops demo`: a made-up fleet so the dashboard can be seen with content.

Nothing here is real. The data is written to its own folder (never to your real state file),
the state file is marked as a demo, and every page shows a "SAMPLE DATA" label while it is open.
It is written through the real HealOps classes, so it also shows the dashboard what a real
state file and audit log look like.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from healops.audit import AuditEvent, AuditStage, JsonlAuditLog, build_event
from healops.events import Incident, Observation
from healops.healing.approval import PendingApproval
from healops.outcomes import ActionResult, PolicyDecision, VerificationResult
from healops.records import ActionProposal, Diagnosis
from healops.store import (
    AWAITING_APPROVAL,
    IN_PROGRESS,
    RESOLVED,
    StateStore,
)
from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
    VerificationOutcome,
)

DEMO_DB = "state.db"
DEMO_AUDIT = "audit.jsonl"
SEED = 20261030
HOURS_OF_HISTORY = 24 * 7

APPS = (
    "market-direction", "sector-banks", "sector-energy", "sector-cement", "sector-fertilizer",
    "sector-power", "sector-autos", "sector-tech", "sector-pharma", "sector-textile",
    "sector-food", "stock-ranker",
)
BASELINE_NAME = "direction_accuracy"
BASELINE = 0.52


@dataclass(frozen=True, slots=True)
class Scenario:
    key: int
    app: str
    kind: IncidentType
    severity: Severity
    summary: str
    signal: tuple[str, ObservationKind, float]
    minutes_ago: float
    reach: str  # detect | diagnose | approval | act | verify | resolved
    cause: str
    action: ActionType
    params: dict[str, str]
    human: bool = False
    heal_minutes: float = 5.0


SCENARIOS = (
    Scenario(142, "sector-energy", IncidentType.DATA_DRIFT, Severity.HIGH,
             "Feature drift on prev_return_5d", ("prev_return_5d", ObservationKind.DRIFT, 0.31),
             19, "diagnose", "the live feature has shifted away from the training window",
             ActionType.REQUEST_RETRAINING, {"window_days": "252"}),
    Scenario(141, "stock-ranker", IncidentType.PERFORMANCE_DEGRADATION, Severity.CRITICAL,
             "Direction accuracy below baseline for 3 days",
             (BASELINE_NAME, ObservationKind.METRIC, 0.489),
             6, "approval", "prev_return_5d has shifted right, which matches the loss of accuracy",
             ActionType.REQUEST_RETRAINING,
             {"training_window": "last 252 market days", "release": "canary at 5 percent traffic",
              "if_it_fails": "automatic rollback to the previous version"}),
    Scenario(140, "sector-cement", IncidentType.DATA_QUALITY, Severity.MEDIUM,
             "Two symbols missing from the daily feed", ("missing_symbols", ObservationKind.METRIC, 2),
             41, "act", "the daily feed skipped two symbols after a vendor retry",
             ActionType.RUN_DIAGNOSTIC, {"check": "backfill missing symbols"}),
    Scenario(139, "sector-cement", IncidentType.DATA_DRIFT, Severity.MEDIUM,
             "Retrain of sector-cement running", ("feature_drift_max", ObservationKind.DRIFT, 0.27),
             120, "verify", "drift on two features after the last market shift",
             ActionType.REQUEST_RETRAINING, {"window_days": "252"}),
    Scenario(143, "sector-energy", IncidentType.HIGH_LATENCY, Severity.LOW,
             "Training job slower than usual", ("latency_ms", ObservationKind.METRIC, 640),
             8, "detect", "", ActionType.NO_ACTION, {}),
    Scenario(138, "market-direction", IncidentType.HIGH_LATENCY, Severity.LOW,
             "Inference latency spike", ("latency_ms", ObservationKind.METRIC, 820),
             300, "resolved", "a cold model process after a restart",
             ActionType.RESTART_COMPONENT, {"component": "model-server"}, heal_minutes=2),
    Scenario(137, "sector-banks", IncidentType.DATA_QUALITY, Severity.MEDIUM,
             "Schema change: new column in the feed", ("schema_changes", ObservationKind.METRIC, 1),
             540, "resolved", "the vendor added a column the pipeline did not expect",
             ActionType.RUN_DIAGNOSTIC, {"check": "schema"}, heal_minutes=3),
    Scenario(136, "market-direction", IncidentType.DATA_DRIFT, Severity.HIGH,
             "Volatility regime shift", ("volatility_20d", ObservationKind.DRIFT, 0.34),
             1500, "resolved", "market volatility moved to a new regime",
             ActionType.REQUEST_RETRAINING, {"window_days": "252"}, human=True, heal_minutes=95),
    Scenario(135, "sector-banks", IncidentType.PERFORMANCE_DEGRADATION, Severity.LOW,
             "Confidence scores poorly calibrated", ("calibration_error", ObservationKind.METRIC, 0.11),
             1700, "resolved", "calibration drifted with the last retrain",
             ActionType.RELOAD_MODEL, {}, heal_minutes=4),
    Scenario(134, "sector-power", IncidentType.HEALTH_FAILURE, Severity.MEDIUM,
             "Training job failed: out of memory", ("training_job_ok", ObservationKind.HEALTH, 0),
             2900, "resolved", "the job ran out of memory on a larger window",
             ActionType.RESTART_COMPONENT, {"component": "trainer"}, heal_minutes=6),
    Scenario(133, "sector-textile", IncidentType.PREDICTION_ANOMALY, Severity.MEDIUM,
             "Predictions all on one side for a day", ("up_share", ObservationKind.METRIC, 0.97),
             4300, "resolved", "a stale feature file was loaded",
             ActionType.RELOAD_MODEL, {}, heal_minutes=3),
    Scenario(132, "sector-food", IncidentType.DATA_DRIFT, Severity.MEDIUM,
             "Feature drift on volume_ratio", ("volume_ratio", ObservationKind.DRIFT, 0.28),
             6000, "resolved", "volume changed after a holiday week",
             ActionType.REQUEST_RETRAINING, {"window_days": "252"}, heal_minutes=8),
)


def _id(rng: random.Random) -> str:
    return f"{rng.getrandbits(128):032x}"


def _obs(app: str, kind: ObservationKind, name: str, value: float, at: datetime) -> Observation:
    return Observation(app_name=app, kind=kind, name=name, value=float(value), timestamp=at)


def _seed_observations(store: StateStore, now: datetime, rng: random.Random) -> None:
    rows: list[Observation] = []
    ramp = {  # app -> (final drift, hours of ramp)
        "sector-energy": (0.31, 30),
        "stock-ranker": (0.19, 40),
        "sector-cement": (0.09, 0),
    }
    for app in APPS:
        end, ramp_hours = ramp.get(app, (0.0, 0))
        for h in range(HOURS_OF_HISTORY, -1, -1):
            at = now - timedelta(hours=h)
            base = 0.03 + rng.random() * 0.04
            if app == "sector-cement":
                # high a day ago, back down after the retrain
                value = base + (0.22 if 16 <= h < 44 else (0.22 * (h - 6) / 10 if 6 < h < 16 else 0))
            elif ramp_hours and h < ramp_hours:
                value = base + (end - base) * (1 - h / ramp_hours)
            else:
                value = base
            rows.append(_obs(app, ObservationKind.DRIFT, "feature_drift_max", round(value, 4), at))
    # the suspect feature behind two incidents
    for h in range(HOURS_OF_HISTORY, -1, -1):
        at = now - timedelta(hours=h)
        value = 0.05 + rng.random() * 0.02 + (0.26 * (1 - h / 30) if h < 30 else 0)
        rows.append(_obs("sector-energy", ObservationKind.DRIFT, "prev_return_5d", round(value, 4), at))
    # accuracy of the ranker: fine, then falling below the baseline in the last 3 days
    for h in range(14 * 24, -1, -1):
        at = now - timedelta(hours=h)
        base = 0.545 + (rng.random() - 0.5) * 0.012
        fall = 0.07 * (1 - h / 72) if h < 72 else 0.0
        rows.append(_obs("stock-ranker", ObservationKind.METRIC, BASELINE_NAME, round(base - fall, 4), at))
    # latency of the market model, with a spike 5 hours ago
    for h in range(48, -1, -1):
        at = now - timedelta(hours=h)
        spike = 700 if 4 <= h <= 5 else 0
        rows.append(_obs("market-direction", ObservationKind.METRIC, "latency_ms",
                         round(105 + rng.random() * 20 + spike, 1), at))
    store.add_observations(rows)


def _events(s: Scenario, incident: Incident, now: datetime, rng: random.Random) -> list[AuditEvent]:
    t0 = incident.detected_at
    minute = timedelta(minutes=1)
    out: list[tuple[datetime, AuditEvent]] = []

    def add(at: datetime, stage: AuditStage, **kw: object) -> None:
        event = build_event(stage, incident.incident_id, incident.app_name, **kw)  # type: ignore[arg-type]
        out.append((at, replace(event, created_at=at)))

    add(t0, AuditStage.INCIDENT_OPENED, record=incident)
    if s.reach == "detect":
        return [e for _, e in out]

    diagnosis = Diagnosis(
        incident_id=incident.incident_id,
        summary=f"{incident.summary}.",
        confidence=0.82 if s.reach != "resolved" or not s.human else 0.55,
        probable_cause=s.cause,
        evidence_used=(f"{s.signal[0]} = {s.signal[2]}",),
        alternatives=_alternatives(s),
    )
    add(t0 + minute, AuditStage.DIAGNOSED, record=diagnosis)
    if s.reach == "diagnose":
        return [e for _, e in out]

    proposal = ActionProposal(
        incident_id=incident.incident_id,
        diagnosis_id=diagnosis.diagnosis_id,
        action_type=s.action,
        rationale=_rationale(s),
        expected_outcome="The signal returns inside its normal band and stays there.",
        params=dict(s.params),
        risks=("The new version may do worse than the old one.",) if s.action is ActionType.REQUEST_RETRAINING else (),
    )
    add(t0 + 2 * minute, AuditStage.PROPOSED, record=proposal)

    needs_person = s.reach == "approval" or s.human
    verdict = PolicyVerdict.NEEDS_APPROVAL if needs_person else PolicyVerdict.ALLOW
    reasons = (
        "Retrain frequency is within the limit of 1 per 7 days.",
        "Training data ends before the locked test period.",
        "A person must approve this action for ranker models."
        if s.app == "stock-ranker"
        else "A person must approve a retrain for this model.",
    ) if needs_person else (f"configuration verdict for {s.action.value} is allow",)
    decision = PolicyDecision(proposal_id=proposal.proposal_id, verdict=verdict, reasons=reasons)
    add(t0 + 3 * minute, AuditStage.POLICY_DECIDED, record=decision)

    if needs_person:
        expires = t0 + timedelta(hours=1)
        add(t0 + 3 * minute, AuditStage.APPROVAL_REQUESTED,
            data={"proposal_id": proposal.proposal_id, "action": s.action.value,
                  "expires_at": expires.isoformat()})
        if s.reach == "approval":
            return [e for _, e in out]
        add(t0 + 6 * minute, AuditStage.APPROVAL_RESOLVED,
            data={"proposal_id": proposal.proposal_id, "approved": True, "approver": "ali"})

    start = t0 + (7 if needs_person else 4) * minute
    add(start, AuditStage.ACTION_STARTING, data={"action": s.action.value, "proposal_id": proposal.proposal_id})
    if s.reach == "act":
        return [e for _, e in out]

    end = t0 + timedelta(minutes=s.heal_minutes - 1) if s.heal_minutes > 8 else start + minute
    end = max(start, end)
    result = ActionResult(
        proposal_id=proposal.proposal_id, dry_run=False, completed=True,
        message=_result_message(s), started_at=start, ended_at=end,
    )
    add(end, AuditStage.ACTION_FINISHED, record=result)
    verification = VerificationResult(
        execution_id=result.execution_id, outcome=VerificationOutcome.VERIFIED,
        checks_passed=(f"{s.signal[0]} is back inside its band",),
        verified_at=end + timedelta(seconds=20),
    )
    add(end + timedelta(seconds=20), AuditStage.VERIFIED, record=verification)
    if s.reach == "resolved":
        add(end + timedelta(seconds=30), AuditStage.RESOLVED, note="recovery verified")
    return [e for _, e in out]


def _alternatives(s: Scenario) -> tuple[str, ...]:
    if s.action is ActionType.REQUEST_RETRAINING:
        return (
            "Roll back to the previous version. Faster, but it was trained on the same old window.",
            "Do nothing and wait. Predictions stay below baseline. Not recommended.",
        )
    return ("Do nothing and wait. The signal would stay outside its band.",)


def _rationale(s: Scenario) -> str:
    if s.action is ActionType.REQUEST_RETRAINING:
        return ("The live feature has shifted and the training window no longer matches it. "
                "A retrain on recent data is the smallest change that addresses it.")
    if s.action is ActionType.RESTART_COMPONENT:
        return "A restart is the smallest change that clears the problem."
    if s.action is ActionType.RELOAD_MODEL:
        return "Reloading the model file clears a stale or damaged copy."
    return "A diagnostic is safe and tells us more before anything is changed."


def _result_message(s: Scenario) -> str:
    return {
        ActionType.REQUEST_RETRAINING: "retraining requested (nothing is deployed automatically)",
        ActionType.RESTART_COMPONENT: "restarted the component",
        ActionType.RELOAD_MODEL: "model reloaded",
    }.get(s.action, "diagnostic finished")


def build_demo(folder: str | Path, now: datetime | None = None) -> tuple[Path, Path]:
    """Write the sample state file and audit log into `folder` (replacing an old demo)."""
    now = now if now is not None else datetime.now(UTC)
    folder = Path(folder)
    if folder.exists():
        marker = folder / DEMO_DB
        if any(folder.iterdir()) and not marker.exists():
            raise FileExistsError(
                f"{folder} already has files that are not a HealOps demo; choose another folder"
            )
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    db_path, audit_path = folder / DEMO_DB, folder / DEMO_AUDIT

    rng = random.Random(SEED)
    store = StateStore(db_path)
    log = JsonlAuditLog(audit_path)
    try:
        _seed_observations(store, now, rng)
        every: list[AuditEvent] = []
        for s in SCENARIOS:
            detected = now - timedelta(minutes=s.minutes_ago)
            name, kind, value = s.signal
            evidence = (_obs(s.app, kind, name, value, detected),)
            state = {
                "resolved": IncidentState.RESOLVED,
                "approval": IncidentState.AWAITING_APPROVAL,
            }.get(s.reach, IncidentState.EXECUTING if s.reach in ("act", "verify") else IncidentState.OPEN)
            incident = Incident(
                app_name=s.app, incident_type=s.kind, severity=s.severity, summary=s.summary,
                evidence=evidence, incident_id=_id(rng), detected_at=detected, state=state,
            )
            events = _events(s, incident, now, rng)
            every.extend(events)
            last = events[-1].created_at
            if s.reach == "resolved":
                status = RESOLVED
            elif s.reach == "approval":
                status = AWAITING_APPROVAL
            else:
                status = IN_PROGRESS
            store.save_incident(incident, status, now=last)
            if s.reach == "approval":
                _save_pending(store, incident, events, detected)
        for event in sorted(every, key=lambda e: e.created_at):
            log.append(event)
        store.beat(now)
    finally:
        store.close()
    _mark_demo(db_path)
    return db_path, audit_path


def _save_pending(store: StateStore, incident: Incident, events: list[AuditEvent], t0: datetime) -> None:
    from healops.serialization import record_from_dict

    def payload(stage: AuditStage) -> dict:
        return next(e.payload for e in events if e.stage is stage)

    proposal = record_from_dict(ActionProposal, payload(AuditStage.PROPOSED))
    decision = record_from_dict(PolicyDecision, payload(AuditStage.POLICY_DECIDED))
    store.save_pending(
        PendingApproval(
            incident=incident, proposal=proposal, decision=decision,
            requested_at=t0 + timedelta(minutes=3), expires_at=t0 + timedelta(hours=1),
        )
    )


def _mark_demo(db_path: Path) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('demo', '1')")
        db.commit()
    finally:
        db.close()


def is_demo(db_path: str | Path) -> bool:
    """True when this state file was written by `healops demo`."""
    try:
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'demo'").fetchone()
        return bool(row and row[0] == "1")
    except sqlite3.Error:
        return False
    finally:
        db.close()
