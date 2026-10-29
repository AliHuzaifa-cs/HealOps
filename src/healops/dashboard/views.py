"""Turn the saved HealOps state into the plain data the dashboard pages show.

Everything here is read-only and takes the time as a parameter, so it is easy to test.
Nothing is invented: a number the state file cannot support is left out (None), and the page
then says so instead of showing a made-up value.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from healops.audit import AuditEvent, AuditStage
from healops.events import Incident, Observation
from healops.healing.approval import PendingApproval
from healops.store import (
    ACKNOWLEDGED,
    AWAITING_APPROVAL,
    ESCALATED,
    IN_PROGRESS,
    RESOLVED,
    StateStore,
    StoredIncident,
)
from healops.types import IncidentType, ObservationKind, PolicyVerdict

RANGES: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_RANGE = "24h"

ACTIVE = frozenset({IN_PROGRESS, AWAITING_APPROVAL, ESCALATED})
CLOSED = frozenset({RESOLVED, ACKNOWLEDGED})

HEARTBEAT_FRESH_SECONDS = 300
MAX_INCIDENTS = 1000
MAX_OBSERVATIONS = 20000
SPARK_POINTS = 12
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# The five stages of the healing loop, and which audit stage puts an incident in which one.
LOOP = (
    ("Detect", "Drift, decay and data checks"),
    ("Diagnose", "Agent finds the likely cause"),
    ("Decide", "Policy check, maybe approval"),
    ("Act", "Retrain, roll back or restart"),
    ("Verify", "Metrics checked after the fix"),
)
_STAGE_OF = {
    AuditStage.INCIDENT_OPENED: 1,
    AuditStage.DIAGNOSED: 2,
    AuditStage.PROPOSED: 3,
    AuditStage.POLICY_DECIDED: 3,
    AuditStage.APPROVAL_REQUESTED: 3,
    AuditStage.ESCALATED: 3,
    AuditStage.APPROVAL_RESOLVED: 4,
    AuditStage.ACTION_STARTING: 4,
    AuditStage.ACTION_FINISHED: 4,
    AuditStage.VERIFIED: 5,
    AuditStage.RESOLVED: 5,
}
_STATE_LABEL = {1: "Detected", 2: "Diagnosing", 3: "Deciding", 4: "Healing", 5: "Verifying"}

_CAUSES = (
    ("Feature drift", {IncidentType.DATA_DRIFT}),
    ("Data quality", {IncidentType.DATA_QUALITY}),
    (
        "Performance decay",
        {IncidentType.PERFORMANCE_DEGRADATION, IncidentType.PREDICTION_ANOMALY},
    ),
    (
        "Infrastructure",
        {IncidentType.HEALTH_FAILURE, IncidentType.HIGH_LATENCY, IncidentType.HIGH_ERROR_RATE},
    ),
)


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z") if moment else None


def short_id(incident_id: str) -> str:
    return "INC-" + incident_id[:6]


def humanize(value: str) -> str:
    text = value.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return None


@dataclass(slots=True)
class Settings:
    """What the server was told about itself (all optional)."""

    workspace: str = "HealOps"
    environment: str = ""
    user: str = "local"
    actions_enabled: bool = False
    dry_run: bool | None = None
    drift_threshold: float | None = None
    metric_thresholds: dict[str, float] = field(default_factory=dict)
    metric_baselines: dict[str, float] = field(default_factory=dict)
    demo: bool = False


@dataclass(slots=True)
class Snapshot:
    """Everything one page needs, read once."""

    now: datetime
    settings: Settings
    incidents: list[StoredIncident]
    pending: list[PendingApproval]
    events: dict[str, list[AuditEvent]]
    all_events: list[AuditEvent]
    observations: list[Observation]
    heartbeat: datetime | None
    audit_ok: bool = True

    def events_for(self, incident_id: str) -> list[AuditEvent]:
        return self.events.get(incident_id, [])


def take_snapshot(
    store: StateStore,
    events: Sequence[AuditEvent],
    settings: Settings,
    now: datetime,
    audit_ok: bool = True,
    horizon: timedelta = RANGES["30d"],
) -> Snapshot:
    by_incident: dict[str, list[AuditEvent]] = defaultdict(list)
    ordered = sorted(events, key=lambda e: e.created_at)
    for event in ordered:
        by_incident[event.incident_id].append(event)
    return Snapshot(
        now=now,
        settings=settings,
        incidents=store.incidents(limit=MAX_INCIDENTS),
        pending=store.pending(),
        events=dict(by_incident),
        all_events=ordered,
        observations=store.observations(since=now - horizon, limit=MAX_OBSERVATIONS),
        heartbeat=store.last_beat(),
        audit_ok=audit_ok,
    )


# ---------- small facts about one incident ----------


def stage_of(snap: Snapshot, stored: StoredIncident) -> int:
    """1..5: where the incident is in the healing loop (0 when it is closed)."""
    if stored.status in CLOSED:
        return 0
    events = snap.events_for(stored.incident.incident_id)
    for event in reversed(events):
        if event.stage in _STAGE_OF:
            return _STAGE_OF[event.stage]
    return 3 if stored.status in (AWAITING_APPROVAL, ESCALATED) else 1


def state_label(snap: Snapshot, stored: StoredIncident) -> tuple[str, str]:
    """(label, tone) for the state column. Tones: warn, heal, ok, bad, info, idle."""
    status = stored.status
    if status == AWAITING_APPROVAL:
        return "Awaiting approval", "warn"
    if status == ESCALATED:
        return "Escalated", "bad"
    if status == RESOLVED:
        return "Resolved", "ok"
    if status == ACKNOWLEDGED:
        return "Acknowledged", "info"
    stage = stage_of(snap, stored)
    tone = "heal" if stage >= 2 else "info"
    return _STATE_LABEL.get(stage, "In progress"), tone


def closed_at(snap: Snapshot, stored: StoredIncident) -> datetime | None:
    if stored.status not in CLOSED:
        return None
    resolved = [
        e.created_at
        for e in snap.events_for(stored.incident.incident_id)
        if e.stage is AuditStage.RESOLVED
    ]
    return max(resolved) if resolved else stored.updated_at


def handled_by(snap: Snapshot, stored: StoredIncident) -> str:
    """'agent', 'human' or 'pending' (still open)."""
    if stored.status in ACTIVE:
        return "pending"
    if stored.status == ACKNOWLEDGED:
        return "human"
    for event in snap.events_for(stored.incident.incident_id):
        if event.stage is AuditStage.APPROVAL_RESOLVED and event.payload.get("approved"):
            approver = str(event.payload.get("approver", ""))
            if approver and not approver.startswith("system:"):
                return "human"
    return "agent"


def seconds_to_heal(snap: Snapshot, stored: StoredIncident) -> float | None:
    end = closed_at(snap, stored)
    if end is None or stored.status != RESOLVED:
        return None
    return max(0.0, (end - stored.incident.detected_at).total_seconds())


def _incident_row(snap: Snapshot, stored: StoredIncident) -> dict[str, Any]:
    incident = stored.incident
    label, tone = state_label(snap, stored)
    return {
        "id": incident.incident_id,
        "short": short_id(incident.incident_id),
        "severity": incident.severity.value,
        "title": incident.summary,
        "app": incident.app_name,
        "type": incident.incident_type.value,
        "status": stored.status,
        "state": label,
        "state_tone": tone,
        "by": handled_by(snap, stored),
        "detected_at": _iso(incident.detected_at),
        "updated_at": _iso(stored.updated_at),
        "closed_at": _iso(closed_at(snap, stored)),
    }


def _in_range(snap: Snapshot, stored: StoredIncident, window: timedelta) -> bool:
    start = snap.now - window
    if stored.status in ACTIVE:
        return True
    end = closed_at(snap, stored) or stored.updated_at
    return end >= start or stored.incident.detected_at >= start


def _window(name: str | None) -> tuple[str, timedelta]:
    key = name if name in RANGES else DEFAULT_RANGE
    return key, RANGES[key]


# ---------- the left sidebar and top strip, sent with every page ----------


def shell(snap: Snapshot) -> dict[str, Any]:
    settings = snap.settings
    apps = _apps(snap)
    open_count = sum(1 for s in snap.incidents if s.status in ACTIVE)
    beat = snap.heartbeat
    age = None if beat is None else max(0, int((snap.now - beat).total_seconds()))
    running = age is not None and age <= HEARTBEAT_FRESH_SECONDS
    if settings.dry_run is True:
        note = "Dry run: it proposes and asks, and changes nothing."
    elif settings.dry_run is False:
        note = "Live: approved and allowed actions are carried out."
    else:
        note = "Acts only as your policy allows."
    return {
        "now": _iso(snap.now),
        "workspace": settings.workspace,
        "environment": settings.environment,
        "user": settings.user,
        "demo": settings.demo,
        "actions_enabled": settings.actions_enabled,
        "audit_ok": snap.audit_ok,
        "agent": {"running": running, "age_s": age, "note": note},
        "badges": {
            "incidents": open_count,
            "models": len(apps),
            "agent": len(snap.pending),
        },
    }


def _apps(snap: Snapshot) -> list[str]:
    names = {s.incident.app_name for s in snap.incidents}
    names.update(o.app_name for o in snap.observations)
    return sorted(names)


def _latest_drift(snap: Snapshot) -> dict[str, float]:
    latest: dict[str, tuple[datetime, float]] = {}
    for o in snap.observations:
        if o.kind is not ObservationKind.DRIFT:
            continue
        value = _num(o.value)
        if value is None:
            continue
        old = latest.get(o.app_name)
        if old is None or o.timestamp >= old[0]:
            latest[o.app_name] = (o.timestamp, value)
    return {app: v for app, (_, v) in latest.items()}


def _app_status(snap: Snapshot, app: str) -> str:
    active = [s for s in snap.incidents if s.incident.app_name == app and s.status in ACTIVE]
    if not active:
        return "Healthy"
    if any(stage_of(snap, s) >= 4 for s in active):
        return "Healing"
    if any(s.incident.incident_type is IncidentType.DATA_DRIFT for s in active):
        return "Drifting"
    return "Degraded"


# ---------- overview page ----------


def _hour_marks(now: datetime, window: timedelta, count: int) -> list[datetime]:
    step = window / count
    return [now - window + step * (i + 1) for i in range(count)]


def _spark(values: Sequence[float | None]) -> list[float] | None:
    clean = [v for v in values if v is not None]
    return list(clean) if len(clean) >= 2 and len(set(clean)) > 1 else None


def overview(snap: Snapshot, range_name: str | None = None) -> dict[str, Any]:
    key, window = _window(range_name)
    now = snap.now
    start = now - window
    active = [s for s in snap.incidents if s.status in ACTIVE]
    apps = _apps(snap)

    counts = Counter(stage_of(snap, s) for s in active)
    loop = [
        {"no": i + 1, "name": name, "note": note, "n": counts.get(i + 1, 0)}
        for i, (name, note) in enumerate(LOOP)
    ]

    statuses = {app: _app_status(snap, app) for app in apps}
    drift = _latest_drift(snap)
    tiles = [
        {
            "name": app,
            "status": statuses[app],
            "drift": None if app not in drift else round(drift[app], 3),
        }
        for app in apps
    ]
    healthy = sum(1 for s in statuses.values() if s == "Healthy")

    resolved_in_window = [
        s
        for s in snap.incidents
        if s.status == RESOLVED and (closed_at(snap, s) or s.updated_at) >= start
    ]
    healed_by_agent = [s for s in resolved_in_window if handled_by(snap, s) == "agent"]
    durations = [d for s in resolved_in_window if (d := seconds_to_heal(snap, s)) is not None]
    earlier = [
        d
        for s in snap.incidents
        if s.status == RESOLVED
        and start - timedelta(days=7) <= (closed_at(snap, s) or s.updated_at) < start
        and (d := seconds_to_heal(snap, s)) is not None
    ]

    marks = _hour_marks(now, window, SPARK_POINTS)

    def open_at(t: datetime) -> list[StoredIncident]:
        out = []
        for s in snap.incidents:
            if s.incident.detected_at > t:
                continue
            end = closed_at(snap, s)
            if end is None or end > t:
                out.append(s)
        return out

    open_series = [float(len(open_at(t))) for t in marks]
    healthy_series = [
        float(len(apps) - len({s.incident.app_name for s in open_at(t)} & set(apps)))
        for t in marks
    ]
    healed_series = [
        float(
            sum(
                1
                for s in resolved_in_window
                if (closed_at(snap, s) or s.updated_at) <= t
            )
        )
        for t in marks
    ]

    attention = len(apps) - healthy
    awaiting = sum(1 for s in active if s.status == AWAITING_APPROVAL)
    kpis = {
        "healthy": {
            "healthy": healthy,
            "total": len(apps),
            "attention": attention,
            "spark": _spark(healthy_series),
        },
        "open": {"n": len(active), "awaiting": awaiting, "spark": _spark(open_series)},
        "healed": {
            "n": len(healed_by_agent),
            "resolved": len(resolved_in_window),
            "spark": _spark(healed_series),
        },
        "mttr": {
            "seconds": (sum(durations) / len(durations)) if durations else None,
            "previous_seconds": (sum(earlier) / len(earlier)) if earlier else None,
            "samples": len(durations),
        },
    }

    ranked = sorted(
        active,
        key=lambda s: (SEVERITY_ORDER[s.incident.severity.value], -s.updated_at.timestamp()),
    )
    live = [_incident_row(snap, s) for s in ranked[:5]]

    activity = [
        {"at": _iso(e.created_at), "text": describe_event(e)} for e in snap.all_events[-4:][::-1]
    ]

    return {
        "range": key,
        "loop": loop,
        "kpis": kpis,
        "tiles": tiles,
        "live": live,
        "drift_pulse": drift_pulse(snap, window),
        "drift_threshold": snap.settings.drift_threshold,
        "activity": activity,
    }


def drift_pulse(snap: Snapshot, window: timedelta, buckets: int = 24) -> list[dict[str, Any]]:
    """The highest drift score in each time bucket across all apps and features."""
    now = snap.now
    step = window / buckets
    start = now - window
    best: list[float | None] = [None] * buckets
    for o in snap.observations:
        if o.kind is not ObservationKind.DRIFT or o.timestamp < start or o.timestamp > now:
            continue
        value = _num(o.value)
        if value is None:
            continue
        index = min(buckets - 1, int((o.timestamp - start) / step))
        if best[index] is None or value > best[index]:
            best[index] = value
    return [
        {"at": _iso(start + step * (i + 1)), "value": None if v is None else round(v, 4)}
        for i, v in enumerate(best)
    ]


# ---------- audit events in words ----------


def _payload_text(event: AuditEvent, *names: str) -> str:
    for name in names:
        value = event.payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


_ACTION_PHRASE = {
    "request_retraining": "retrain",
    "restart_component": "restart",
    "reload_model": "reload the model",
    "rollback_model": "roll back the model",
    "request_redeployment": "redeploy",
    "run_diagnostic": "run a diagnostic",
    "escalate": "escalate",
    "no_action": "take no action",
}


def _action(event: AuditEvent) -> str:
    raw = _payload_text(event, "action_type", "action") or "action"
    return _ACTION_PHRASE.get(raw, humanize(raw).lower())


def describe_event(event: AuditEvent) -> str:
    app = event.app_name
    stage = event.stage
    if stage is AuditStage.INCIDENT_OPENED:
        return f"Opened an incident for {app}: {_payload_text(event, 'summary') or 'detected'}."
    if stage is AuditStage.DIAGNOSED:
        return f"Diagnosed {app}: {_payload_text(event, 'summary') or 'no summary'}"
    if stage is AuditStage.PROPOSED:
        return f"Proposed to {_action(event)} on {app}."
    if stage is AuditStage.POLICY_DECIDED:
        verdict = humanize(_payload_text(event, "verdict") or "decided").lower()
        return f"Policy check for {app}: {verdict}."
    if stage is AuditStage.APPROVAL_REQUESTED:
        return f"Asked a person to approve ({_action(event)}) on {app}."
    if stage is AuditStage.APPROVAL_RESOLVED:
        who = _payload_text(event, "approver") or "someone"
        verdict = "approved" if event.payload.get("approved") else "turned down"
        return f"{who} {verdict} the action on {app}."
    if stage is AuditStage.ACTION_STARTING:
        return f"Started {_action(event)} on {app}."
    if stage is AuditStage.ACTION_FINISHED:
        return f"Finished on {app}: {_payload_text(event, 'message') or 'done'}"
    if stage is AuditStage.VERIFIED:
        outcome = humanize(_payload_text(event, "outcome") or "checked").lower()
        return f"Verified {app} after the fix: {outcome}."
    if stage is AuditStage.ESCALATED:
        return f"Escalated {app} to a person. {event.note or ''}".strip()
    if stage is AuditStage.RESOLVED:
        return f"Closed the incident on {app}: {event.note or 'recovery verified'}."
    return f"{stage.value} on {app}"


# ---------- incidents page ----------


def incidents_page(
    snap: Snapshot, range_name: str | None = None, limit: int = 200
) -> dict[str, Any]:
    key, window = _window(range_name)
    visible = [s for s in snap.incidents if _in_range(snap, s, window)]
    rows = [_incident_row(snap, s) for s in visible[:limit]]

    open_now = [s for s in visible if s.status in ACTIVE]
    resolved = [s for s in visible if s.status in CLOSED]
    counts = {
        "open": len(open_now),
        "awaiting": sum(1 for s in open_now if s.status == AWAITING_APPROVAL),
        "by_agent": sum(1 for s in resolved if handled_by(snap, s) == "agent"),
        "by_human": sum(1 for s in resolved if handled_by(snap, s) == "human"),
    }

    causes = []
    totals = [
        sum(1 for s in visible if s.incident.incident_type in kinds) for _, kinds in _CAUSES
    ]
    biggest = max(totals) if totals and max(totals) else 1
    for (label, _), n in zip(_CAUSES, totals, strict=True):
        causes.append({"label": label, "n": n, "w": round(100 * n / biggest)})

    return {
        "range": key,
        "counts": counts,
        "rows": rows,
        "causes": causes,
        "note": _agent_note(visible),
    }


def _agent_note(visible: Sequence[StoredIncident]) -> str | None:
    """The signal that shows up in the most incidents, in one honest sentence."""
    seen: Counter[str] = Counter()
    for s in visible:
        names = {o.name for o in s.incident.evidence}
        seen.update(names)
    if not seen:
        return None
    name, n = seen.most_common(1)[0]
    if n < 2:
        return None
    return (
        f"The signal behind the most incidents in this period is {name} ({n} of "
        f"{len(visible)}). Worth checking that signal itself, not only the models that use it."
    )


def incidents_csv(snap: Snapshot, range_name: str | None = None) -> str:
    _, window = _window(range_name)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["id", "severity", "title", "app", "type", "status", "handled_by", "detected_at", "closed_at"]
    )
    for stored in snap.incidents:
        if not _in_range(snap, stored, window):
            continue
        row = _incident_row(snap, stored)
        writer.writerow(
            [
                row["id"],
                row["severity"],
                _csv_safe(row["title"]),
                _csv_safe(row["app"]),
                row["type"],
                row["status"],
                row["by"],
                row["detected_at"],
                row["closed_at"] or "",
            ]
        )
    return out.getvalue()


def _csv_safe(text: str) -> str:
    """Stop a spreadsheet from running a cell that starts like a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# ---------- one incident ----------

_KIND_OF_STAGE = {
    AuditStage.INCIDENT_OPENED: "OBSERVE",
    AuditStage.DIAGNOSED: "HYPOTHESIS",
    AuditStage.PROPOSED: "CONCLUSION",
    AuditStage.POLICY_DECIDED: "POLICY",
    AuditStage.APPROVAL_REQUESTED: "POLICY",
    AuditStage.APPROVAL_RESOLVED: "POLICY",
    AuditStage.ACTION_STARTING: "TOOL",
    AuditStage.ACTION_FINISHED: "TOOL",
    AuditStage.VERIFIED: "CONCLUSION",
    AuditStage.ESCALATED: "POLICY",
    AuditStage.RESOLVED: "CONCLUSION",
}


def _tool_line(event: AuditEvent) -> str:
    p = event.payload
    stage = event.stage
    if stage is AuditStage.INCIDENT_OPENED:
        names = sorted({str(o.get("name")) for o in p.get("evidence", []) if isinstance(o, Mapping)})
        return f"detector.evaluate → {p.get('incident_type', 'incident')}" + (
            f" ({', '.join(names)})" if names else ""
        )
    if stage is AuditStage.DIAGNOSED:
        conf = _num(p.get("confidence"))
        return "agent.diagnose" + (f" → confidence {conf:.2f}" if conf is not None else "")
    if stage is AuditStage.PROPOSED:
        params = p.get("params") or {}
        inner = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"plan.create(type={p.get('action_type', '?')}" + (f", {inner}" if inner else "") + ")"
    if stage is AuditStage.POLICY_DECIDED:
        return f"policy.evaluate → {p.get('verdict', '?')}"
    if stage is AuditStage.APPROVAL_REQUESTED:
        return f"approvals.add → expires {str(p.get('expires_at', '?'))[:16].replace('T', ' ')}"
    if stage is AuditStage.APPROVAL_RESOLVED:
        return "approvals.resolve → " + ("approved" if p.get("approved") else "not approved")
    if stage is AuditStage.ACTION_STARTING:
        return f"action.start({p.get('action', p.get('action_type', '?'))})"
    if stage is AuditStage.ACTION_FINISHED:
        mode = "dry run" if p.get("dry_run") else "live"
        return f"action.finish → {mode}, completed={p.get('completed')}"
    if stage is AuditStage.VERIFIED:
        return f"verify.postconditions → {p.get('outcome', '?')}"
    return stage.value


def _timeline(events: Iterable[AuditEvent]) -> list[dict[str, Any]]:
    out = []
    for event in events:
        text = describe_event(event)
        stage = event.stage
        if stage is AuditStage.DIAGNOSED:
            cause = _payload_text(event, "probable_cause")
            summary = _payload_text(event, "summary")
            text = (f"{summary} Likely cause: {cause}." if cause else summary) or text
        elif stage is AuditStage.PROPOSED:
            text = _payload_text(event, "rationale") or text
        elif stage is AuditStage.POLICY_DECIDED:
            reasons = event.payload.get("reasons") or []
            text = ("; ".join(str(r).rstrip(". ") for r in reasons) + ".") if reasons else text
        elif stage is AuditStage.INCIDENT_OPENED:
            text = _payload_text(event, "summary") or text
        elif stage is AuditStage.ACTION_FINISHED:
            text = _payload_text(event, "message") or text
        out.append(
            {
                "at": _iso(event.created_at),
                "kind": _KIND_OF_STAGE.get(event.stage, "TOOL"),
                "text": text,
                "tool": _tool_line(event),
            }
        )
    return out


def _stepper(snap: Snapshot, stored: StoredIncident, events: Sequence[AuditEvent]) -> list[dict]:
    names = [name for name, _ in LOOP]
    stage = stage_of(snap, stored)
    closed = stored.status in CLOSED
    times: dict[int, datetime] = {}
    for event in events:
        number = _STAGE_OF.get(event.stage)
        if number is not None:
            times[number] = event.created_at
    steps = []
    for i, name in enumerate(names, start=1):
        if closed or i < stage:
            state = "done"
        elif i == stage:
            state = "current"
        else:
            state = "todo"
        if stored.status == AWAITING_APPROVAL and i == 3:
            sub = "needs you"
        elif state == "done":
            sub = "done"
        elif state == "current":
            sub = "in progress"
        else:
            sub = "not started"
        steps.append({"name": name, "state": state, "sub": sub, "at": _iso(times.get(i))})
    return steps


def _checks(decision_verdict: PolicyVerdict, reasons: Sequence[str]) -> list[dict[str, str]]:
    out = []
    for reason in reasons:
        text = reason.lower()
        if decision_verdict is PolicyVerdict.DENY:
            tag = "DENY"
        elif decision_verdict is PolicyVerdict.NEEDS_APPROVAL and (
            "must approve" in text or "needs_approval" in text or "person" in text
        ):
            tag = "HOLD"
        else:
            tag = "PASS"
        out.append({"tag": tag, "text": reason})
    return out


def _series(snap: Snapshot, stored: StoredIncident) -> dict[str, Any]:
    incident = stored.incident
    name = incident.evidence[0].name
    kind = incident.evidence[0].kind
    age = max(timedelta(0), snap.now - incident.detected_at)
    lookback = min(timedelta(days=14), max(timedelta(hours=72), age * 2))
    start = incident.detected_at - lookback
    points = [
        {"at": _iso(o.timestamp), "value": v}
        for o in snap.observations
        if o.app_name == incident.app_name
        and o.name == name
        and o.timestamp >= start
        and (v := _num(o.value)) is not None
    ]
    settings = snap.settings
    if kind is ObservationKind.DRIFT:
        threshold = settings.drift_threshold
    else:
        threshold = settings.metric_thresholds.get(name)
    return {
        "name": name,
        "kind": kind.value,
        "points": points[-500:],
        "threshold": threshold,
        "baseline": settings.metric_baselines.get(name),
        "detected_at": _iso(incident.detected_at),
        "lookback_hours": round(lookback.total_seconds() / 3600),
    }


def incident_detail(snap: Snapshot, incident_id: str) -> dict[str, Any] | None:
    stored = next((s for s in snap.incidents if s.incident.incident_id == incident_id), None)
    if stored is None:
        return None
    incident: Incident = stored.incident
    events = snap.events_for(incident_id)
    pending = next((p for p in snap.pending if p.incident.incident_id == incident_id), None)

    proposal = decision = None
    alternatives: list[str] = []
    for event in events:
        if event.stage is AuditStage.PROPOSED:
            proposal = event.payload
        elif event.stage is AuditStage.POLICY_DECIDED:
            decision = event.payload
        elif event.stage is AuditStage.DIAGNOSED:
            alternatives = [str(a) for a in event.payload.get("alternatives") or []]

    action: dict[str, Any] | None = None
    checks: list[dict[str, str]] = []
    if pending is not None:
        p = pending.proposal
        action = {
            "type": p.action_type.value,
            "title": humanize(p.action_type.value),
            "rationale": p.rationale,
            "expected": p.expected_outcome,
            "params": dict(sorted(p.params.items())),
            "risks": list(p.risks),
            "proposal_id": p.proposal_id,
            "expires_at": _iso(pending.expires_at),
            "can_decide": True,
        }
        checks = _checks(pending.decision.verdict, pending.decision.reasons)
    elif proposal is not None:
        action = {
            "type": str(proposal.get("action_type", "")),
            "title": humanize(str(proposal.get("action_type", "action"))),
            "rationale": str(proposal.get("rationale", "")),
            "expected": str(proposal.get("expected_outcome", "")),
            "params": dict(sorted((proposal.get("params") or {}).items())),
            "risks": [str(r) for r in proposal.get("risks") or []],
            "proposal_id": str(proposal.get("proposal_id", "")),
            "expires_at": None,
            "can_decide": False,
        }
    if not checks and decision is not None:
        try:
            verdict = PolicyVerdict(decision.get("verdict"))
        except ValueError:
            verdict = PolicyVerdict.ALLOW
        checks = _checks(verdict, [str(r) for r in decision.get("reasons") or []])

    row = _incident_row(snap, stored)
    row["dry_run"] = any(
        e.stage is AuditStage.ACTION_FINISHED and e.payload.get("dry_run") for e in events
    )
    return {
        "incident": row,
        "steps": _stepper(snap, stored, events),
        "series": _series(snap, stored),
        "evidence": [
            {"name": o.name, "kind": o.kind.value, "value": _num(o.value), "at": _iso(o.timestamp)}
            for o in incident.evidence
        ],
        "timeline": _timeline(events),
        "action": action,
        "checks": checks,
        "alternatives": alternatives,
        "reason": stored.reason,
    }
