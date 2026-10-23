"""The `healops` command: look at what is waiting, and answer from another terminal.

    healops list
    healops approve <incident-id> --by ali
    healops reject  <incident-id> --by ali --reason "not now"

The command never touches your application. It writes your answer to the state file;
the running HealOps picks it up on its next check and carries it out through the usual
policy, audit and verification path. Anyone who can write to the state file can answer
approvals, so protect that file like you would protect the application itself.
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from healops.exceptions import ConfigError, HealOpsError
from healops.healing.approval import PendingApproval
from healops.loader import load_config
from healops.store import (
    APPLIED,
    APPROVE,
    ESCALATED,
    REJECT,
    WAITING,
    StateStore,
)

DEFAULT_DB = "healops_state.db"
DEFAULT_WAIT_SECONDS = 120.0
STALE_AFTER_SECONDS = 300.0
POLL_SECONDS = 0.5
RECENT_ALERTS = 5

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_YET = 3


def _age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m ago"


def _until(moment: datetime, now: datetime) -> str:
    seconds = int((moment - now).total_seconds())
    if seconds <= 0:
        return "expired"
    if seconds < 3600:
        return f"in {seconds // 60}m {seconds % 60}s"
    return f"in {seconds // 3600}h {seconds % 3600 // 60}m"


def _find_pending(store: StateStore, wanted: str) -> PendingApproval:
    """The one waiting approval whose incident id is `wanted` (or starts with it)."""
    if len(wanted) < 4:
        raise LookupError("give at least 4 characters of the incident id")
    waiting = store.pending()
    exact = [p for p in waiting if p.incident.incident_id == wanted]
    matches = exact or [p for p in waiting if p.incident.incident_id.startswith(wanted)]
    if not matches:
        raise LookupError(f"nothing is waiting for approval for incident {wanted!r}")
    if len(matches) > 1:
        raise LookupError(f"{wanted!r} matches more than one incident; use more characters")
    return matches[0]


def _db_path(args: argparse.Namespace) -> str:
    if args.db:
        return str(args.db)
    if args.config:
        config = load_config(args.config)
        if config.state_db_path is None:
            raise ConfigError("the config keeps its state in memory (state_db_path is null)")
        return config.state_db_path
    return DEFAULT_DB


def _open(args: argparse.Namespace) -> StateStore:
    path = _db_path(args)
    if not Path(path).is_file():
        raise FileNotFoundError(
            f"no state file at {path!r}. Run HealOps first, or point to it with --db or --config"
        )
    return StateStore(path)


def _heartbeat_note(store: StateStore, now: datetime) -> str:
    beat = store.last_beat()
    note = f"HealOps last active: {_age(beat, now)}"
    if beat is None or (now - beat).total_seconds() > STALE_AFTER_SECONDS:
        note += "  (WARNING: it may not be running, so nothing will carry out your answer)"
    return note


def cmd_list(args: argparse.Namespace, out: Callable[[str], None], now: datetime) -> int:
    store = _open(args)
    try:
        out(_heartbeat_note(store, now))
        waiting = store.pending()
        out("")
        out(f"Waiting for approval ({len(waiting)}):")
        for p in waiting:
            params = ", ".join(f"{k}={v}" for k, v in sorted(p.proposal.params.items()))
            out(f"  {p.incident.incident_id}  [{p.incident.app_name}] {p.incident.summary}")
            out(
                f"      action: {p.proposal.action_type.value}"
                + (f" ({params})" if params else "")
                + f"   expires {_until(p.expires_at, now)}"
            )
            if p.proposal.rationale:
                out(f"      why: {p.proposal.rationale}")
        if not waiting:
            out("  nothing")
        escalated = store.incidents([ESCALATED])
        out("")
        out(f"Escalated, still open ({len(escalated)}):")
        for e in escalated:
            out(f"  {e.incident.incident_id}  [{e.incident.app_name}] {e.incident.summary}")
            if e.reason:
                out(f"      reason: {e.reason}")
        if not escalated:
            out("  nothing")
        recent = store.alerts(limit=RECENT_ALERTS)
        out("")
        out(f"Recent alerts (newest {RECENT_ALERTS}):")
        for alert in recent:
            tag = "[dry-run] " if alert.dry_run else ""
            out(f"  {_age(alert.created_at, now)}  {alert.kind.value}  [{alert.app_name}] {tag}{alert.title}")
        if not recent:
            out("  nothing")
        queued = store.waiting_decisions()
        if queued:
            out("")
            out(f"Answers not yet picked up by HealOps ({len(queued)}):")
            for d in queued:
                out(f"  #{d.decision_id} {d.action} {d.incident_id} by {d.approver}")
    finally:
        store.close()
    return EXIT_OK


def _answer(
    args: argparse.Namespace,
    action: str,
    out: Callable[[str], None],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    store = _open(args)
    try:
        pending = _find_pending(store, args.incident)
        decision_id = store.add_decision(
            pending.incident.incident_id,
            pending.proposal.proposal_id,
            action,
            args.by,
            getattr(args, "reason", None),
            now=now(),
        )
        verb = "approval" if action == APPROVE else "rejection"
        out(f"recorded your {verb} (#{decision_id}) for {pending.incident.incident_id}")
        out(_heartbeat_note(store, now()))
        if args.no_wait:
            return EXIT_OK

        out(f"waiting up to {args.timeout:g}s for HealOps to carry it out...")
        deadline = time.monotonic() + args.timeout
        while True:
            decision = store.decision(decision_id)
            if decision is not None and decision.status != WAITING:
                break
            if time.monotonic() >= deadline:
                out(
                    "HealOps has not picked it up yet. Your answer is saved and will be applied "
                    "on its next check (unless the approval expires first)."
                )
                return EXIT_NOT_YET
            sleep(POLL_SECONDS)

        if decision.status == APPLIED:
            out(f"done: the incident is now {decision.outcome}")
            return EXIT_OK
        out(f"not applied: {decision.outcome}")
        return EXIT_FAILED
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="healops", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", help=f"state file (default: {DEFAULT_DB})")
        p.add_argument("--config", help="read the state file path from this config file")

    listing = sub.add_parser("list", help="show approvals waiting and escalated incidents")
    common(listing)

    for name, text in (("approve", "approve a waiting action"), ("reject", "refuse a waiting action")):
        p = sub.add_parser(name, help=text)
        p.add_argument("incident", help="incident id (or its first characters)")
        p.add_argument("--by", required=True, help="your name, recorded in the audit log")
        p.add_argument("--no-wait", action="store_true", help="do not wait for the result")
        p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_SECONDS)
        if name == "reject":
            p.add_argument("--reason", help="why (optional)")
        common(p)
    return parser


def main(
    argv: Sequence[str] | None = None,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args, out, now())
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        return _answer(args, APPROVE if args.command == "approve" else REJECT, out, now, sleep)
    except (LookupError, ValueError, FileNotFoundError, HealOpsError) as error:
        out(f"error: {error}")
        return EXIT_USAGE


def run() -> None:
    sys.exit(main())