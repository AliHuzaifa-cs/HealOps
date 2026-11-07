"""The `healops dashboard` and `healops demo` commands."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import webbrowser
from collections.abc import Callable
from pathlib import Path

from healops.config import HealOpsConfig
from healops.exceptions import ConfigError
from healops.loader import load_config

DEFAULT_DB = "healops_state.db"
DEFAULT_PORT = 8765
DEMO_FOLDER = "healops_demo"
EXIT_OK = 0
EXIT_USAGE = 2


def add_parsers(sub: argparse._SubParsersAction) -> None:
    dash = sub.add_parser("dashboard", help="open the dashboard on this computer")
    dash.add_argument("--db", help=f"state file (default: {DEFAULT_DB})")
    dash.add_argument("--config", help="read the state and audit paths and the limits from this config file")
    dash.add_argument("--audit", help="audit log (default: found next to the state file, or from the config)")
    dash.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    dash.add_argument("--port", type=int, default=DEFAULT_PORT)
    dash.add_argument("--by", default=None, help="your name, recorded when you answer an approval")
    dash.add_argument("--name", default=None, help="workspace name shown in the sidebar")
    dash.add_argument("--env", default="", help="environment label shown in the sidebar, e.g. production")
    dash.add_argument(
        "--allow-actions",
        action="store_true",
        help="let this page approve or reject waiting actions (only on this computer)",
    )
    dash.add_argument(
        "--allow-host", action="append", default=[], help="extra Host name to accept (repeatable)"
    )
    dash.add_argument("--open", action="store_true", help="open the page in your browser")

    demo = sub.add_parser("demo", help="open the dashboard with labelled sample data")
    demo.add_argument("--dir", default=DEMO_FOLDER, help=f"folder for the sample data (default: {DEMO_FOLDER})")
    demo.add_argument("--port", type=int, default=DEFAULT_PORT)
    demo.add_argument("--no-open", action="store_true", help="do not open the page in your browser")


def _is_local(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _find_audit(args: argparse.Namespace, config: HealOpsConfig | None, db: Path) -> Path | None:
    if args.audit:
        return Path(args.audit)
    if config is not None and config.audit_log_path:
        return Path(config.audit_log_path)
    for candidate in (db.parent / "audit.jsonl", db.parent / "healops_audit.jsonl", Path("healops_audit.jsonl")):
        if candidate.is_file():
            return candidate
    return None


def _settings(args: argparse.Namespace, config: HealOpsConfig | None, demo: bool):
    from healops.dashboard.views import Settings

    settings = Settings(
        workspace=args.name or "HealOps",
        environment=args.env,
        user=args.by or getpass.getuser(),
        actions_enabled=bool(args.allow_actions),
        demo=demo,
    )
    if config is not None:
        settings.dry_run = config.dry_run
        settings.drift_threshold = config.thresholds.drift_score
        for name, value in (
            ("latency_ms", config.thresholds.latency_ms),
            ("error_rate", config.thresholds.error_rate),
        ):
            if value is not None:
                settings.metric_thresholds[name] = value
        settings.metric_baselines = {name: m.baseline for name, m in config.metric_degradation.items()}
        settings.agent = {
            "mode": "Dry run" if config.dry_run else "Live",
            "diagnosis": f"{config.llm.provider} / {config.llm.model}" if config.llm else "rules only",
            "check_interval_seconds": config.check_interval_seconds,
            "consecutive_breaches": config.thresholds.consecutive_breaches,
            "min_confidence_for_auto": config.min_confidence_for_auto,
            "max_recovery_attempts": config.max_recovery_attempts,
            "recovery_checks": config.recovery_checks,
            "approval_timeout_seconds": config.approval_timeout_seconds,
            "email_alerts": config.email is not None,
        }
        settings.policies = [
            {
                "action": "any other action",
                "verdict": config.default_policy.verdict.value,
                "max_retries": config.default_policy.max_retries,
                "cooldown_seconds": config.default_policy.cooldown_seconds,
            }
        ] + [
            {
                "action": action.value,
                "verdict": policy.verdict.value,
                "max_retries": policy.max_retries,
                "cooldown_seconds": policy.cooldown_seconds,
            }
            for action, policy in sorted(config.policies.items(), key=lambda kv: kv[0].value)
        ]
    return settings


def _run(app, host: str, port: int, out: Callable[[str], None], open_page: bool) -> int:
    from healops.dashboard.server import serve

    try:
        server = serve(app, host, port)
    except OSError as error:
        out(f"error: cannot listen on {host}:{port} ({error.strerror or error}). Try --port.")
        app.close()
        return EXIT_USAGE
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    url = f"http://{shown}:{port}/"
    out(f"HealOps dashboard: {url}   (press Ctrl+C to stop)")
    if open_page:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        out("stopped")
    finally:
        server.server_close()
        app.close()
    return EXIT_OK


def run_dashboard(args: argparse.Namespace, out: Callable[[str], None] = print) -> int:
    from healops.dashboard.server import DashboardApp

    config = None
    try:
        if args.config:
            config = load_config(args.config)
    except ConfigError as error:
        out(f"error: {error}")
        return EXIT_USAGE

    db = Path(args.db or (config.state_db_path if config and config.state_db_path else DEFAULT_DB))
    if not db.is_file():
        out(
            f"error: no state file at {str(db)!r}. Run HealOps first, or point to it with --db or --config. "
            "To look around with sample data, run: healops demo"
        )
        return EXIT_USAGE
    if args.allow_actions and not _is_local(args.host):
        out("error: --allow-actions is only allowed when listening on this computer (127.0.0.1), because the page has no login yet")
        return EXIT_USAGE
    if not 0 < args.port < 65536:
        out("error: --port must be between 1 and 65535")
        return EXIT_USAGE

    audit = _find_audit(args, config, db)
    out(f"state file: {db}")
    out(f"audit log:  {audit if audit else 'none found (step-by-step histories will be empty; use --audit)'}")
    if not _is_local(args.host):
        out("warning: this page has no login yet. Anyone who can reach this address can read your incidents.")
    if args.allow_actions:
        out(f"approvals from this page are ON, recorded as {args.by or getpass.getuser()!r}")

    app = DashboardApp(
        db,
        audit,
        _settings(args, config, demo=False),
        extra_hosts=frozenset(args.allow_host),
    )
    return _run(app, args.host, args.port, out, args.open)


def run_demo(args: argparse.Namespace, out: Callable[[str], None] = print) -> int:
    from healops.dashboard.demo import build_demo
    from healops.dashboard.server import DashboardApp

    try:
        db, audit = build_demo(args.dir)
    except FileExistsError as error:
        out(f"error: {error}")
        return EXIT_USAGE
    out(f"sample data written to {Path(args.dir)} (not your real state file)")
    ns = argparse.Namespace(
        name="psx-intelligence", env="sample data", by=None, allow_actions=True, audit=None
    )
    settings = _settings(ns, None, demo=True)
    settings.user = "demo"
    settings.drift_threshold = 0.25
    settings.metric_baselines = {"direction_accuracy": 0.52}
    app = DashboardApp(db, audit, settings)
    return _run(app, "127.0.0.1", args.port, out, not args.no_open)
