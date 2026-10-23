"""Saved state: what HealOps needs to remember across restarts, and what a dashboard shows.

One SQLite file (standard library only). It holds observations (for charts),
incidents (latest known state) and approvals that are waiting for a person.
It is *not* the audit log: the audit log is the append-only record of what
happened, this is the current picture.
"""

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from healops.events import Incident, Observation
from healops.exceptions import StateStoreError
from healops.healing.approval import PendingApproval
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal
from healops.serialization import record_from_json, record_to_json
from healops.types import ObservationKind

log = logging.getLogger("healops.store")

SCHEMA_VERSION = 2
MEMORY = ":memory:"

# Incident statuses kept in the store.
IN_PROGRESS = "in_progress"
AWAITING_APPROVAL = "awaiting_approval"
ESCALATED = "escalated"
RESOLVED = "resolved"
ACKNOWLEDGED = "acknowledged"
STATUSES = frozenset({IN_PROGRESS, AWAITING_APPROVAL, ESCALATED, RESOLVED, ACKNOWLEDGED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    is_bool INTEGER NOT NULL,
    ts TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_by_series ON observations (app_name, name, ts);
CREATE INDEX IF NOT EXISTS observations_by_time ON observations (ts);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    incident_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_by_status ON incidents (status, updated_at);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    approver TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS decisions_by_status ON decisions (status, id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
    incident_id TEXT PRIMARY KEY,
    incident_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def _stamp(moment: datetime) -> str:
    """A fixed-width UTC text form, so text order is time order."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


APPROVE = "approve"
REJECT = "reject"
DECISION_ACTIONS = frozenset({APPROVE, REJECT})

WAITING = "waiting"
APPLIED = "applied"
FAILED = "failed"

MAX_APPROVER_LENGTH = 100
MAX_REASON_LENGTH = 500


@dataclass(frozen=True, slots=True)
class Decision:
    """A person's answer to a waiting approval, written by the command line and applied by the
    running HealOps (only the running process holds the connection to the application)."""

    decision_id: int
    incident_id: str
    proposal_id: str
    action: str
    approver: str
    reason: str | None
    created_at: datetime
    status: str
    outcome: str | None
    finished_at: datetime | None


def _check_text(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    return value.strip()


@dataclass(frozen=True, slots=True)
class StoredIncident:
    """An incident as last saved, with where it stands."""

    incident: Incident
    status: str
    reason: str | None
    updated_at: datetime


class StateStore:
    """SQLite-backed state. Safe to share between threads; one connection, one lock."""

    def __init__(self, path: str | Path = MEMORY, retention_days: int = 30) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        self._retention = timedelta(days=retention_days)
        self._lock = threading.Lock()
        self._path = str(path)
        try:
            if self._path != MEMORY:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._prepare()
        except sqlite3.Error as error:
            raise StateStoreError(f"cannot open the state file ({type(error).__name__})") from None

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _prepare(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StateStoreError(
                f"the state file has schema version {version}, newer than this HealOps "
                f"understands ({SCHEMA_VERSION}); it was not touched"
            )
        if version == 0:
            tables = self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            if tables:
                raise StateStoreError("the file is a database but not a HealOps state file")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    def _run(self, work):
        with self._lock:
            try:
                result = work(self._db)
                self._db.commit()
                return result
            except sqlite3.Error as error:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass  # the connection itself is gone; the original error is what matters
                raise StateStoreError(f"state store failed ({type(error).__name__})") from None

    # ----- observations -----

    def add_observations(self, observations: Iterable[Observation]) -> int:
        rows = []
        for o in observations:
            number = float(o.value)
            finite = math.isfinite(number)
            rows.append(
                (
                    o.app_name,
                    o.kind.value,
                    o.name,
                    number if finite else None,
                    1 if isinstance(o.value, bool) else 0,
                    _stamp(o.timestamp),
                    json.dumps(o.details, sort_keys=True),
                )
            )
        if not rows:
            return 0
        self._run(
            lambda db: db.executemany(
                "INSERT INTO observations (app_name, kind, name, value, is_bool, ts, details) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        )
        return len(rows)

    def observations(
        self,
        app_name: str | None = None,
        name: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[Observation]:
        """The newest matching observations, returned oldest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        where, args = [], []
        if app_name is not None:
            where.append("app_name = ?")
            args.append(app_name)
        if name is not None:
            where.append("name = ?")
            args.append(name)
        if since is not None:
            where.append("ts >= ?")
            args.append(_stamp(since))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            "SELECT app_name, kind, name, value, is_bool, ts, details FROM observations "
            f"{clause} ORDER BY ts DESC, id DESC LIMIT ?"
        )
        rows = self._run(lambda db: db.execute(sql, [*args, limit]).fetchall())
        found = []
        for app, kind, series, value, is_bool, ts, details in reversed(rows):
            number = float("nan") if value is None else value
            found.append(
                Observation(
                    app_name=app,
                    kind=ObservationKind(kind),
                    name=series,
                    value=bool(number) if is_bool else number,
                    timestamp=_parse(ts),
                    details=json.loads(details),
                )
            )
        return found

    def prune(self, now: datetime) -> int:
        """Delete observations older than the retention period. Incidents are kept."""
        cutoff = _stamp(now - self._retention)
        return self._run(
            lambda db: db.execute("DELETE FROM observations WHERE ts < ?", (cutoff,)).rowcount
        )

    # ----- incidents -----

    def save_incident(
        self, incident: Incident, status: str, reason: str | None = None, now: datetime | None = None
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown incident status {status!r}")
        stamp = _stamp(now if now is not None else datetime.now(UTC))
        self._run(
            lambda db: db.execute(
                "INSERT INTO incidents (incident_id, app_name, status, reason, incident_json, "
                "updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET "
                "status=excluded.status, reason=excluded.reason, "
                "incident_json=excluded.incident_json, updated_at=excluded.updated_at",
                (
                    incident.incident_id,
                    incident.app_name,
                    status,
                    reason,
                    record_to_json(incident),
                    stamp,
                ),
            )
        )

    def incidents(
        self, statuses: Sequence[str] | None = None, limit: int = 200
    ) -> list[StoredIncident]:
        """Saved incidents, newest update first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        args: list = []
        clause = ""
        if statuses is not None:
            if not statuses:
                return []
            clause = f"WHERE status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql = (
            "SELECT incident_json, status, reason, updated_at FROM incidents "
            f"{clause} ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        )
        rows = self._run(lambda db: db.execute(sql, [*args, limit]).fetchall())
        return [
            StoredIncident(record_from_json(Incident, text), status, reason, _parse(updated))
            for text, status, reason, updated in rows
        ]

    # ----- approvals -----

    def save_pending(self, pending: PendingApproval) -> None:
        self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO approvals (incident_id, incident_json, proposal_json, "
                "decision_json, requested_at, expires_at) VALUES (?,?,?,?,?,?)",
                (
                    pending.incident.incident_id,
                    record_to_json(pending.incident),
                    record_to_json(pending.proposal),
                    record_to_json(pending.decision),
                    _stamp(pending.requested_at),
                    _stamp(pending.expires_at),
                ),
            )
        )

    def delete_pending(self, incident_id: str) -> None:
        self._run(lambda db: db.execute("DELETE FROM approvals WHERE incident_id = ?", (incident_id,)))

    def pending(self) -> list[PendingApproval]:
        rows = self._run(
            lambda db: db.execute(
                "SELECT incident_json, proposal_json, decision_json, requested_at, expires_at "
                "FROM approvals ORDER BY requested_at, incident_id"
            ).fetchall()
        )
        return [
            PendingApproval(
                record_from_json(Incident, incident),
                record_from_json(ActionProposal, proposal),
                record_from_json(PolicyDecision, decision),
                _parse(requested),
                _parse(expires),
            )
            for incident, proposal, decision, requested, expires in rows
        ]

    # ----- decisions (people answering approvals from outside the process) -----

    def add_decision(
        self,
        incident_id: str,
        proposal_id: str,
        action: str,
        approver: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Record an answer. Refused if one for the same incident is still waiting."""
        if action not in DECISION_ACTIONS:
            raise ValueError(f"action must be one of {sorted(DECISION_ACTIONS)}")
        approver = _check_text(approver, "approver", MAX_APPROVER_LENGTH)
        if reason is not None:
            reason = _check_text(reason, "reason", MAX_REASON_LENGTH)
        stamp = _stamp(now if now is not None else datetime.now(UTC))

        def work(db: sqlite3.Connection) -> int:
            if db.execute(
                "SELECT 1 FROM decisions WHERE incident_id = ? AND status = ?",
                (incident_id, WAITING),
            ).fetchone():
                raise ValueError("a decision for this incident is already waiting to be applied")
            cursor = db.execute(
                "INSERT INTO decisions (incident_id, proposal_id, action, approver, reason, "
                "created_at, status) VALUES (?,?,?,?,?,?,?)",
                (incident_id, proposal_id, action, approver, reason, stamp, WAITING),
            )
            return int(cursor.lastrowid)

        return self._run(work)

    def waiting_decisions(self) -> list[Decision]:
        """Decisions not yet applied, oldest first."""
        return self._decisions("WHERE status = ? ORDER BY id", (WAITING,))

    def decision(self, decision_id: int) -> Decision | None:
        found = self._decisions("WHERE id = ?", (decision_id,))
        return found[0] if found else None

    def finish_decision(
        self, decision_id: int, status: str, outcome: str, now: datetime | None = None
    ) -> None:
        if status not in (APPLIED, FAILED):
            raise ValueError("status must be 'applied' or 'failed'")
        stamp = _stamp(now if now is not None else datetime.now(UTC))
        self._run(
            lambda db: db.execute(
                "UPDATE decisions SET status = ?, outcome = ?, finished_at = ? "
                "WHERE id = ? AND status = ?",
                (status, outcome[:500], stamp, decision_id, WAITING),
            )
        )

    def _decisions(self, tail: str, args: tuple) -> list[Decision]:
        sql = (
            "SELECT id, incident_id, proposal_id, action, approver, reason, created_at, "
            f"status, outcome, finished_at FROM decisions {tail}"
        )
        rows = self._run(lambda db: db.execute(sql, args).fetchall())
        return [
            Decision(
                row[0], row[1], row[2], row[3], row[4], row[5], _parse(row[6]), row[7], row[8],
                _parse(row[9]) if row[9] else None,
            )
            for row in rows
        ]

    # ----- heartbeat -----

    def beat(self, now: datetime | None = None) -> None:
        """Note that HealOps is running (so a command line can tell if anything will answer)."""
        stamp = _stamp(now if now is not None else datetime.now(UTC))
        self._run(
            lambda db: db.execute(
                "INSERT INTO meta (key, value) VALUES ('heartbeat', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (stamp,),
            )
        )

    def last_beat(self) -> datetime | None:
        row = self._run(
            lambda db: db.execute("SELECT value FROM meta WHERE key = 'heartbeat'").fetchone()
        )
        return _parse(row[0]) if row else None