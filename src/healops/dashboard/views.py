"""Turn the saved HealOps state into the plain data the dashboard pages show.

Everything here is read-only and takes the time as a parameter, so it is easy to test.
Nothing is invented: a number the state file cannot support is left out (None), and the page
then says so instead of showing a made-up value.
"""

from __future__ import annotations

import csv
import io
import math
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
    RECOVERED,
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
CLOSED = frozenset({RESOLVED, ACKNOWLEDGED, RECOVERED})

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
    if isinstance(value, (int, float)) and not math.isnan(value):
        return float(value)
    return None


@dataclass(slots=True)
class Settings:
    """What the server was told about itself (all optional)."""

    workspace: str = "HealOps"
    environment: str = ""
    user: str = "local"
    actions_enabled: bool = False
    action_token: str = ""  # when set, every approval must send it
    dry_run: bool | None = None
    drift_threshold: float | None = None
    metric_thresholds: dict[str, float] = field(default_factory=dict)
    metric_baselines: dict[str, float] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)  # how the agent is set up
    policies: list[dict[str, Any]] = field(default_factory=list)  # one row per action kind
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
    store: StateStore | None = None

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
        store=store,
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
        return "Closed by a person", "info"
    if status == RECOVERED:
        return "Recovered", "idle"
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
    """'agent', 'human', 'self' (the problem went away on its own) or 'pending' (still open)."""
    if stored.status in ACTIVE:
        return "pending"
    if stored.status == ACKNOWLEDGED:
        return "human"
    if stored.status == RECOVERED:
        return "self"
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
        "needs_token": bool(settings.action_token),
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
        "by_self": sum(1 for s in resolved if handled_by(snap, s) == "self"),
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
        "can_resolve": stored.status == ESCALATED,
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


# ---------- drift and service signals ----------

SIGNAL_POINTS = 120
HEATMAP_DAYS = 14
MAX_HEATMAP_ROWS = 30
MAX_SERVICE_CHARTS = 16
DATA_PREFIX = "data."
FEATURE_PREFIX = "feature_psi."  # a metric the application sends for display only: it opens no incident
DEFAULT_DRIFT_LIMIT = 0.25
_BUCKET_SECONDS = {"24h": 900, "7d": 7200, "30d": 21600}
_KIND_ORDER = {"health": 0, "metric": 1}


def _downsample(points: list[tuple[datetime, float]], count: int, worst_low: bool) -> list[dict]:
    """At most `count` points; each keeps the worst reading of its slice, so a spike survives."""
    if len(points) <= count:
        return [{"at": _iso(t), "value": round(v, 6)} for t, v in points]
    size = len(points) / count
    out = []
    for i in range(count):
        chunk = points[int(i * size) : int((i + 1) * size)] or [points[-1]]
        pick = (min if worst_low else max)(chunk, key=lambda p: p[1])
        out.append({"at": _iso(chunk[-1][0]), "value": round(pick[1], 6)})
    return out


def _tile(label, value, note, tag, tone) -> dict[str, str]:
    return {"label": label, "value": value, "note": note, "tag": tag, "tone": tone}


def _days_old(days: float) -> str:
    n = max(0, round(days))
    return "Today" if n == 0 else ("1 day old" if n == 1 else f"{n} days old")


def _check_tiles(values: dict[str, float]) -> list[dict[str, str]]:
    """The four data checks, from the `data.*` readings the application sends (if it does)."""
    none = ("NO DATA", "idle")
    tiles = []

    schema = values.get("data.schema_ok")
    if schema is None:
        tiles.append(_tile("Schema", "Not reported", "The application does not send this check", *none))
    elif schema >= 1:
        tiles.append(_tile("Schema", "Matches", "Columns are what the model expects", "PASS", "ok"))
    else:
        tiles.append(_tile("Schema", "Changed", "Columns differ from what the model expects", "FAIL", "bad"))

    age = values.get("data.freshness_days")
    if age is None:
        tiles.append(_tile("Freshness", "Not reported", "The application does not send this check", *none))
    else:
        tag, tone = ("PASS", "ok") if age <= 4 else (("WARN", "warn") if age <= 7 else ("FAIL", "bad"))
        tiles.append(_tile("Freshness", _days_old(age), "Latest daily bar received", tag, tone))

    missing, total = values.get("data.missing_symbols"), values.get("data.symbols_total")
    if missing is None:
        tiles.append(_tile("Missing symbols", "Not reported", "The application does not send this check", *none))
    else:
        text = f"{missing:g} of {total:g}" if total else f"{missing:g}"
        if missing == 0:
            tag, tone = "PASS", "ok"
        elif total and missing / total <= 0.1:
            tag, tone = "WARN", "warn"
        else:
            tag, tone = ("WARN", "warn") if not total else ("FAIL", "bad")
        note = "Every symbol has its latest row" if missing == 0 else "Symbols with no row for the latest day"
        tiles.append(_tile("Missing symbols", text, note, tag, tone))

    bad = values.get("data.out_of_range")
    if bad is None:
        tiles.append(_tile("Values out of range", "Not reported", "The application does not send this check", *none))
    else:
        tiles.append(
            _tile(
                "Values out of range",
                f"{bad:g}",
                "All values inside limits" if bad == 0 else "Prices or volumes outside sane limits",
                "PASS" if bad == 0 else "WARN",
                "ok" if bad == 0 else "warn",
            )
        )
    return tiles


def _heat_name(kind: str, name: str) -> str | None:
    """The heat map row a reading belongs to: a drift score, or a per-feature PSI metric."""
    if kind == "drift":
        return name
    if kind == "metric" and name.startswith(FEATURE_PREFIX):
        return name[len(FEATURE_PREFIX):]
    return None


def signals_page(
    snap: Snapshot, range_name: str | None = None, app: str | None = None
) -> dict[str, Any]:
    """Data checks, a 14-day drift heat map, and a chart per health/speed/error signal."""
    key, window = _window(range_name)
    apps = _apps(snap)
    app = app if app in apps else None
    store = snap.store
    latest = store.latest_readings() if store is not None else []
    latest = [r for r in latest if app is None or r.app_name == app]

    # ---- data checks: newest `data.*` value, whichever application sent it
    values: dict[str, tuple[datetime, float]] = {}
    for r in latest:
        if r.name.startswith(DATA_PREFIX) and (r.name not in values or r.at >= values[r.name][0]):
            values[r.name] = (r.at, r.value)
    checks = _check_tiles({k: v for k, (_, v) in values.items()})

    # ---- heat map: one row per drift signal, one cell per day (its highest reading)
    today = int(snap.now.timestamp() // 86400)
    first = today - (HEATMAP_DAYS - 1)
    days = [_iso(datetime.fromtimestamp((first + i) * 86400, UTC)) for i in range(HEATMAP_DAYS)]
    cells: dict[tuple[str, str], list[float | None]] = {}
    if store is not None:
        for b in store.series_buckets(datetime.fromtimestamp(first * 86400, UTC), 86400):
            name = _heat_name(b.kind, b.name)
            if name is None or (app is not None and b.app_name != app):
                continue
            index = int(b.start.timestamp() // 86400) - first
            if 0 <= index < HEATMAP_DAYS:
                cells.setdefault((b.app_name, name), [None] * HEATMAP_DAYS)[index] = round(b.high, 4)
    newest = {
        (r.app_name, _heat_name(r.kind, r.name)): r.value
        for r in latest
        if _heat_name(r.kind, r.name) is not None
    }
    rows = [
        {
            "app": a,
            "name": n,
            "cells": c,
            "latest": None if (a, n) not in newest else round(newest[(a, n)], 4),
        }
        for (a, n), c in cells.items()
    ]
    rows.sort(key=lambda r: (-(r["latest"] if r["latest"] is not None else -1), r["app"], r["name"]))
    limit = snap.settings.drift_threshold or DEFAULT_DRIFT_LIMIT

    # ---- charts for health, speed, errors and the other metrics
    watched = set(snap.settings.metric_thresholds) | set(snap.settings.metric_baselines)
    service: list[dict[str, Any]] = []
    if store is not None:
        grouped: dict[tuple[str, str, str], list] = {}
        for b in store.series_buckets(snap.now - window, _BUCKET_SECONDS[key]):
            if b.kind == "drift" or b.name.startswith((DATA_PREFIX, FEATURE_PREFIX)):
                continue
            if app is not None and b.app_name != app:
                continue
            grouped.setdefault((b.app_name, b.kind, b.name), []).append(b)
        newest_by = {(r.app_name, r.kind, r.name): r for r in latest}
        for (a, kind, name), buckets in grouped.items():
            health = kind == "health"
            threshold = None if health else snap.settings.metric_thresholds.get(name)
            readings = sum(b.count for b in buckets)
            lows = [b.low for b in buckets]
            highs = [b.high for b in buckets]
            now_reading = newest_by.get((a, kind, name))
            if health:
                over = sum(1 for b in buckets if b.low < 1)
            elif threshold is not None:
                over = sum(1 for b in buckets if b.high > threshold)
            else:
                over = 0
            service.append(
                {
                    "app": a,
                    "kind": kind,
                    "name": name,
                    "label": humanize(name),
                    "latest": buckets[-1].last if now_reading is None else now_reading.value,
                    "latest_at": _iso(buckets[-1].start if now_reading is None else now_reading.at),
                    "min": min(lows),
                    "max": max(highs),
                    "avg": sum(b.mean * b.count for b in buckets) / readings,
                    "readings": readings,
                    "threshold": threshold,
                    "breaches": over,
                    "watched": name in watched or health or threshold is not None,
                    "points": _downsample(
                        [(b.start + timedelta(seconds=_BUCKET_SECONDS[key]), b.low if health else b.high) for b in buckets],
                        SIGNAL_POINTS,
                        worst_low=health,
                    ),
                }
            )
    service.sort(key=lambda s: (not s["watched"], _KIND_ORDER.get(s["kind"], 9), s["app"], s["name"]))
    more = max(0, len(service) - MAX_SERVICE_CHARTS)

    drift_incident = next(
        (
            s.incident.incident_id
            for s in snap.incidents
            if s.status in ACTIVE
            and s.incident.incident_type is IncidentType.DATA_DRIFT
            and (app is None or s.incident.app_name == app)
        ),
        None,
    )
    return {
        "range": key,
        "app": app,
        "apps": apps,
        "checks": checks,
        "heatmap": {"days": days, "rows": rows[:MAX_HEATMAP_ROWS], "hidden": max(0, len(rows) - MAX_HEATMAP_ROWS), "limit": limit},
        "service": service[:MAX_SERVICE_CHARTS],
        "more_service": more,
        "related_incident": drift_incident,
    }


# ---------- the applications HealOps watches ----------


def apps_page(snap: Snapshot) -> dict[str, Any]:
    day_ago = snap.now - timedelta(hours=24)
    latest_drift = _latest_drift(snap)
    rows = []
    newest_first = sorted(snap.all_events, key=lambda e: e.created_at, reverse=True)
    for app in _apps(snap):
        mine = [s for s in snap.incidents if s.incident.app_name == app]
        seen = [o for o in snap.observations if o.app_name == app]
        closed = [s for s in mine if s.status in CLOSED]
        its_events = [e for e in newest_first if e.app_name == app]
        retrain = next(
            (
                e
                for e in its_events
                if e.stage is AuditStage.ACTION_STARTING
                and _payload_text(e, "action_type", "action") == "request_retraining"
            ),
            None,
        )
        changes = [
            {"at": _iso(e.created_at), "text": describe_event(e)}
            for e in its_events
            if e.stage is AuditStage.ACTION_FINISHED
        ][:4]
        rows.append(
            {
                "app": app,
                "last_retrain": _iso(retrain.created_at) if retrain else None,
                "changes": changes,
                "status": _app_status(snap, app),
                "last_seen": _iso(max((o.timestamp for o in seen), default=None)),
                "readings_24h": sum(1 for o in seen if o.timestamp >= day_ago),
                "signals": sorted({o.name for o in seen}),
                "drift": None if app not in latest_drift else round(latest_drift[app], 3),
                "incidents": {
                    "total": len(mine),
                    "open": sum(1 for s in mine if s.status in ACTIVE),
                    "by_agent": sum(1 for s in closed if handled_by(snap, s) == "agent"),
                    "by_human": sum(1 for s in closed if handled_by(snap, s) == "human"),
                    "by_self": sum(1 for s in closed if handled_by(snap, s) == "self"),
                },
            }
        )
    return {
        "apps": rows,
        "gates": _release_gates(snap.settings),
        "limits": {
            "drift": snap.settings.drift_threshold,
            "metrics": dict(snap.settings.metric_thresholds),
        },
    }


def _release_gates(settings: Settings) -> list[dict[str, str]]:
    """The rules a retrain has to pass, taken from the policies the agent really runs with."""
    gates: list[dict[str, str]] = []
    by_action = {p["action"]: p for p in settings.policies}
    retrain = by_action.get("request_retraining") or by_action.get("any other action")
    if retrain is None:
        return gates
    verdict = retrain["verdict"]
    if verdict == "needs_approval":
        gates.append({"label": "A person approves every retrain", "tag": "REQUIRED", "tone": "ok"})
    elif verdict == "allow":
        gates.append({"label": "Retrains run without asking", "tag": "ALLOWED", "tone": "warn"})
    else:
        gates.append({"label": "Retrains never run", "tag": "BLOCKED", "tone": "bad"})
    gates.append(
        {
            "label": f"At most {retrain['max_retries']} {'try' if retrain['max_retries'] == 1 else 'tries'} per incident",
            "tag": "LIMIT",
            "tone": "ok",
        }
    )
    if retrain["cooldown_seconds"]:
        gates.append(
            {
                "label": f"Wait {_span(retrain['cooldown_seconds'])} between tries",
                "tag": "LIMIT",
                "tone": "ok",
            }
        )
    if settings.dry_run is True:
        gates.append({"label": "Dry run: nothing is deployed", "tag": "ON", "tone": "warn"})
    return gates


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0 and seconds >= 86400:
        days = seconds // 86400
        return f"{days} day" + ("" if days == 1 else "s")
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour" + ("" if hours == 1 else "s")
    return f"{max(1, seconds // 60)} min"


# ---------- the agent: how it is set up, and everything it did ----------


def agent_page(snap: Snapshot, query: str | None = None, limit: int = 100) -> dict[str, Any]:
    needle = (query or "").strip().lower()[:100]
    events = sorted(snap.all_events, key=lambda e: e.created_at, reverse=True)
    rows = []
    matched = 0
    for event in events:
        text = describe_event(event)
        haystack = " ".join(
            [text, event.app_name, event.stage.value, short_id(event.incident_id)]
        ).lower()
        if needle and needle not in haystack:
            continue
        matched += 1
        if len(rows) < limit:
            rows.append(
                {
                    "at": _iso(event.created_at),
                    "stage": humanize(event.stage.value),
                    "app": event.app_name,
                    "incident": short_id(event.incident_id),
                    "incident_id": event.incident_id,
                    "text": text,
                }
            )
    waiting = [
        {
            "id": s.incident.incident_id,
            "short": short_id(s.incident.incident_id),
            "title": s.incident.summary,
            "severity": s.incident.severity.value,
            "app": s.incident.app_name,
            "opened": _iso(s.incident.detected_at),
        }
        for s in sorted(snap.incidents, key=lambda s: s.incident.detected_at, reverse=True)
        if s.status == AWAITING_APPROVAL
    ]
    return {
        "waiting": waiting,
        "audit_ok": snap.audit_ok,
        "dry_run": snap.settings.dry_run,
        "setup": dict(snap.settings.agent),
        "policies": list(snap.settings.policies),
        "events": rows,
        "matched": matched,
        "total": len(events),
        "query": needle,
    }


# ---------- integrations: what HealOps is connected to ----------


def integrations_page(snap: Snapshot) -> dict[str, Any]:
    apps = _apps(snap)
    last = max((o.timestamp for o in snap.observations), default=None)
    interval = snap.settings.agent.get("check_interval_seconds")
    fresh_for = max(900, 3 * int(interval)) if isinstance(interval, (int, float)) else 3600
    live = last is not None and (snap.now - last).total_seconds() <= fresh_for
    return {
        "adapter": {
            "apps": apps,
            "last_seen": _iso(last),
            "status": "connected" if live else ("silent" if apps else "none"),
        },
        "email": snap.settings.agent.get("email_alerts"),
        "actions_enabled": snap.settings.actions_enabled,
        "user": snap.settings.user,
        "dry_run": snap.settings.dry_run,
    }
