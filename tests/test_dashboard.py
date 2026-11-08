"""The dashboard: what it shows (from real HealOps records) and how it protects itself."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from healops.audit import JsonlAuditLog
from healops.cli import main as cli_main
from healops.dashboard import views
from healops.dashboard.demo import build_demo, is_demo
from healops.dashboard.server import DashboardApp
from healops.store import StateStore
from healops.testing.harness import detect, heal, make_toy_system
from healops.testing.toy_app import Fault

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
LOCAL = {"Host": "localhost:8765"}


def get(app, path, headers=LOCAL):
    response = app.dispatch("GET", path, headers)
    body = response.body
    try:
        return response.status, json.loads(body)
    except ValueError:
        return response.status, body


def post(app, path, payload, headers=None):
    headers = {**LOCAL, "Content-Type": "application/json", **(headers or {})}
    response = app.dispatch("POST", path, headers, json.dumps(payload).encode())
    return response.status, json.loads(response.body)


@pytest.fixture()
def demo(tmp_path):
    db, audit = build_demo(tmp_path / "demo", now=NOW)
    settings = views.Settings(demo=True, user="tester", actions_enabled=True, drift_threshold=0.25)
    app = DashboardApp(db, audit, settings, clock=lambda: NOW)
    yield app, db
    app.close()


def ranker_id(app):
    _, page = get(app, "/api/incidents?range=7d")
    return next(r["id"] for r in page["rows"] if r["app"] == "stock-ranker")


# ---------- what it shows ----------


def test_demo_state_is_marked_and_separate(demo):
    _, db = demo
    assert is_demo(db)
    assert not is_demo(db.parent / "missing.db")


def test_overview_numbers_add_up(demo):
    app, _ = demo
    status, page = get(app, "/api/overview?range=24h")
    assert status == 200
    assert page["shell"]["demo"] is True
    assert page["kpis"]["open"]["n"] == 5
    assert page["kpis"]["open"]["awaiting"] == 1
    assert sum(stage["n"] for stage in page["loop"]) == 5
    assert len(page["tiles"]) == 12
    healthy = [t for t in page["tiles"] if t["status"] == "Healthy"]
    assert page["kpis"]["healthy"]["healthy"] == len(healthy) == 9
    states = {t["name"]: t["status"] for t in page["tiles"]}
    assert states["stock-ranker"] == "Degraded"
    assert states["sector-energy"] == "Drifting"
    assert states["sector-cement"] == "Healing"
    assert page["live"][0]["severity"] == "critical"
    assert len(page["activity"]) == 4
    assert len(page["drift_pulse"]) == 24


def test_incidents_page_counts_and_filters_by_range(demo):
    app, _ = demo
    _, week = get(app, "/api/incidents?range=7d")
    _, day = get(app, "/api/incidents?range=24h")
    assert week["counts"]["open"] == 5
    assert week["counts"]["by_human"] == 1
    assert len(week["rows"]) > len(day["rows"])
    assert {c["label"] for c in week["causes"]} == {
        "Feature drift", "Data quality", "Performance decay", "Infrastructure",
    }
    human = [r for r in week["rows"] if r["by"] == "human"]
    assert len(human) == 1 and human[0]["title"] == "Volatility regime shift"


def test_incident_detail_has_plan_checks_and_ordered_timeline(demo):
    app, _ = demo
    status, detail = get(app, f"/api/incidents/{ranker_id(app)}")
    assert status == 200
    assert detail["incident"]["state"] == "Awaiting approval"
    assert [s["state"] for s in detail["steps"]] == ["done", "done", "current", "todo", "todo"]
    assert detail["action"]["can_decide"] is True
    assert detail["action"]["title"] == "Request retraining"
    tags = [c["tag"] for c in detail["checks"]]
    assert tags[-1] == "HOLD" and set(tags[:-1]) == {"PASS"}
    kinds = [t["kind"] for t in detail["timeline"]]
    assert kinds[0] == "OBSERVE" and kinds[1] == "HYPOTHESIS"
    times = [t["at"] for t in detail["timeline"]]
    assert times == sorted(times)
    assert detail["series"]["baseline"] is None  # demo settings here set no baseline
    assert len(detail["series"]["points"]) > 10


def test_resolved_incident_is_closed_with_who_healed_it(demo):
    app, _ = demo
    _, page = get(app, "/api/incidents?range=7d")
    done = next(r for r in page["rows"] if r["title"] == "Inference latency spike")
    _, detail = get(app, f"/api/incidents/{done['id']}")
    assert detail["incident"]["by"] == "agent"
    assert all(s["state"] == "done" for s in detail["steps"])
    assert detail["action"]["can_decide"] is False


def test_unknown_incident_is_404(demo):
    app, _ = demo
    status, body = get(app, "/api/incidents/nope")
    assert status == 404 and "error" in body


def test_empty_state_shows_zeros_not_errors(tmp_path):
    db = tmp_path / "empty.db"
    StateStore(db).close()
    app = DashboardApp(db, None, clock=lambda: NOW)
    status, page = get(app, "/api/overview")
    assert status == 200
    assert page["tiles"] == [] and page["live"] == [] and page["activity"] == []
    assert page["kpis"]["mttr"]["seconds"] is None
    assert page["shell"]["demo"] is False
    assert page["shell"]["agent"]["running"] is False
    status, page = get(app, "/api/incidents")
    assert status == 200 and page["rows"] == [] and page["note"] is None
    app.close()


def test_real_orchestrator_output_is_readable(tmp_path):
    """Records written by the real HealOps parts (not demo data) show up correctly."""
    system = make_toy_system(config={
        **__import__("healops.testing.harness", fromlist=["DEFAULT_CONFIG"]).DEFAULT_CONFIG,
        "policies": {},
    })
    system.app.inject(Fault.DRIFT)
    incident = detect(system)[0]
    result = heal(system, incident)
    db, audit_path = tmp_path / "state.db", tmp_path / "audit.jsonl"
    store = StateStore(db)
    store.save_incident(result.incident, "awaiting_approval")
    for pending in system.orchestrator._approvals.pending():
        store.save_pending(pending)
    log = JsonlAuditLog(audit_path)
    for event in system.audit.events:
        log.append(event)
    store.close()

    app = DashboardApp(db, audit_path, clock=lambda: datetime.now(UTC))
    status, page = get(app, "/api/incidents")
    assert status == 200 and len(page["rows"]) == 1
    status, detail = get(app, f"/api/incidents/{page['rows'][0]['id']}")
    assert status == 200
    assert detail["timeline"], "the real audit events become a timeline"
    assert detail["timeline"][0]["kind"] == "OBSERVE"
    assert detail["incident"]["app"] == "toy-app"
    app.close()


def test_csv_neutralises_spreadsheet_formulas(tmp_path):
    from healops.events import Incident, Observation
    from healops.types import IncidentType, ObservationKind, Severity

    db = tmp_path / "s.db"
    store = StateStore(db)
    evil = Incident(
        app_name="=cmd|' /C calc'!A0",
        incident_type=IncidentType.DATA_DRIFT,
        severity=Severity.LOW,
        summary="+SUM(1,1)",
        evidence=(Observation("a", ObservationKind.DRIFT, "d", 0.5),),
    )
    store.save_incident(evil, "in_progress")
    store.close()
    app = DashboardApp(db, None)
    response = app.dispatch("GET", "/api/incidents.csv?range=30d", LOCAL)
    lines = response.body.decode().splitlines()
    assert response.status == 200 and "text/csv" in response.headers["Content-Type"]
    assert "'+SUM(1,1)" in lines[1] and "'=cmd" in lines[1]
    app.close()


# ---------- how it protects itself ----------


def test_foreign_host_header_is_refused(demo):
    app, _ = demo
    for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example"):
        assert app.dispatch("GET", "/api/shell", {"Host": host}).status == 403
    assert app.dispatch("GET", "/api/shell", {"Host": "127.0.0.1:1"}).status == 200
    assert app.dispatch("GET", "/api/shell", {"Host": "[::1]:8765"}).status == 200


def test_extra_hosts_must_be_named(tmp_path):
    db = tmp_path / "s.db"
    StateStore(db).close()
    app = DashboardApp(db, None, extra_hosts=frozenset({"dash.internal"}))
    assert app.dispatch("GET", "/api/shell", {"Host": "dash.internal:80"}).status == 200
    assert app.dispatch("GET", "/api/shell", {"Host": "other.internal"}).status == 403
    app.close()


def test_static_paths_cannot_escape_the_folder(demo, tmp_path):
    app, _ = demo
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    for path in ("/../../secret.txt", "/%2e%2e/%2e%2e/secret.txt", f"/{secret}"):
        response = app.dispatch("GET", path, LOCAL)
        assert b"TOPSECRET" not in response.body


def test_page_files_have_the_types_browsers_need(demo):
    app, _ = demo
    index = app.dispatch("GET", "/", LOCAL)
    assert index.headers["Content-Type"].startswith("text/html")
    script = next(p for p in Path(app._static).glob("assets/*.js")).name
    assert app.dispatch("GET", f"/assets/{script}", LOCAL).headers["Content-Type"].startswith("text/javascript")
    font = next(p for p in Path(app._static).glob("assets/*.woff2")).name
    assert app.dispatch("GET", f"/assets/{font}", LOCAL).headers["Content-Type"] == "font/woff2"


def test_unknown_method_and_post_to_pages_refused(demo):
    app, _ = demo
    assert app.dispatch("DELETE", "/api/incidents", LOCAL).status == 405
    assert app.dispatch("POST", "/", {**LOCAL, "Content-Type": "application/json"}, b"{}").status == 405


def test_answers_are_off_unless_enabled(tmp_path):
    db, audit = build_demo(tmp_path / "d", now=NOW)
    app = DashboardApp(db, audit, views.Settings(actions_enabled=False), clock=lambda: NOW)
    status, body = post(app, f"/api/incidents/{ranker_id(app)}/decision", {"action": "approve"})
    assert status == 403 and "--allow-actions" in body["error"]
    assert StateStore(db).waiting_decisions() == []
    app.close()


def test_approve_is_saved_like_the_command_line_does(demo):
    app, db = demo
    incident_id = ranker_id(app)
    status, body = post(app, f"/api/incidents/{incident_id}/decision", {"action": "approve"})
    assert status == 200 and "saved" in body["message"]
    waiting = StateStore(db).waiting_decisions()
    assert len(waiting) == 1
    assert (waiting[0].incident_id, waiting[0].action, waiting[0].approver) == (incident_id, "approve", "tester")
    # a second answer while the first is still waiting is refused
    status, body = post(app, f"/api/incidents/{incident_id}/decision", {"action": "reject"})
    assert status == 400 and "already waiting" in body["error"]


def test_reject_keeps_the_reason(demo):
    app, db = demo
    status, _ = post(app, f"/api/incidents/{ranker_id(app)}/decision", {"action": "reject", "reason": "not now"})
    assert status == 200
    assert StateStore(db).waiting_decisions()[0].reason == "not now"


@pytest.mark.parametrize(
    ("payload", "headers", "expected"),
    [
        ({"action": "approve"}, {"Origin": "http://evil.example"}, 403),
        ({"action": "delete"}, {}, 400),
        ({"action": "reject", "reason": "x" * 501}, {}, 400),
        ({"action": "reject", "reason": 5}, {}, 400),
        ({"action": "approve"}, {"Content-Type": "text/plain"}, 415),
    ],
)
def test_bad_answers_are_refused(demo, payload, headers, expected):
    app, db = demo
    status, _ = post(app, f"/api/incidents/{ranker_id(app)}/decision", payload, headers)
    assert status == expected
    assert StateStore(db).waiting_decisions() == []


def test_cannot_answer_an_incident_that_is_not_waiting(demo):
    app, _ = demo
    _, page = get(app, "/api/incidents?range=7d")
    resolved = next(r["id"] for r in page["rows"] if r["status"] == "resolved")
    status, _ = post(app, f"/api/incidents/{resolved}/decision", {"action": "approve"})
    assert status == 409


def test_oversized_and_broken_bodies(demo):
    app, _ = demo
    path = f"/api/incidents/{ranker_id(app)}/decision"
    headers = {**LOCAL, "Content-Type": "application/json"}
    assert app.dispatch("POST", path, headers, b"x" * 9000).status == 413
    assert app.dispatch("POST", path, headers, b"{not json").status == 400
    assert app.dispatch("POST", path, headers, b"[1]").status == 400


def test_secrets_stay_hidden_end_to_end(tmp_path):
    """HealOps hides secrets when it writes the audit log; the page must show the hidden form."""
    from healops.audit import AuditStage, build_event
    from healops.events import Incident, Observation
    from healops.types import IncidentType, ObservationKind, Severity

    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    incident = Incident(
        app_name="svc",
        incident_type=IncidentType.HEALTH_FAILURE,
        severity=Severity.HIGH,
        summary=f"login failed with api_key={secret}",
        evidence=(Observation("svc", ObservationKind.HEALTH, "up", False),),
    )
    db, audit = tmp_path / "s.db", tmp_path / "a.jsonl"
    store = StateStore(db)
    store.save_incident(incident, "escalated")
    store.close()
    JsonlAuditLog(audit).append(build_event(AuditStage.INCIDENT_OPENED, incident.incident_id, "svc", record=incident))
    JsonlAuditLog(audit).append(
        build_event(AuditStage.ESCALATED, incident.incident_id, "svc", note=f"token={secret}")
    )
    app = DashboardApp(db, audit)
    status, detail = get(app, f"/api/incidents/{incident.incident_id}")
    assert status == 200
    timeline = json.dumps(detail["timeline"])
    assert secret not in timeline and secret not in audit.read_text()
    assert detail["timeline"][-1]["kind"] == "POLICY"
    app.close()


def test_damaged_audit_log_does_not_break_the_page(demo, tmp_path):
    app, _ = demo
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n")
    damaged = DashboardApp(app._store.path, bad, clock=lambda: NOW)
    status, page = get(damaged, "/api/overview")
    assert status == 200 and page["shell"]["audit_ok"] is False
    damaged.close()


def test_heartbeat_tells_whether_the_agent_runs(demo):
    app, db = demo
    _, shell = get(app, "/api/shell")
    assert shell["agent"]["running"] is True
    late = DashboardApp(db, None, clock=lambda: NOW + timedelta(minutes=30))
    _, shell = get(late, "/api/shell")
    assert shell["agent"]["running"] is False
    late.close()


# ---------- the commands ----------


def test_dashboard_command_needs_a_state_file(tmp_path):
    lines: list[str] = []
    code = cli_main(["dashboard", "--db", str(tmp_path / "nope.db")], out=lines.append)
    assert code == 2 and "healops demo" in " ".join(lines)


def test_actions_are_refused_on_a_network_address(tmp_path):
    db = tmp_path / "s.db"
    StateStore(db).close()
    lines: list[str] = []
    code = cli_main(["dashboard", "--db", str(db), "--allow-actions", "--host", "0.0.0.0"], out=lines.append)
    assert code == 2 and "no login" in " ".join(lines)


def test_demo_never_overwrites_someones_folder(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "important.txt").write_text("keep me")
    lines: list[str] = []
    code = cli_main(["demo", "--dir", str(folder), "--no-open"], out=lines.append)
    assert code == 2
    assert (folder / "important.txt").read_text() == "keep me"


def test_an_escalated_incident_can_be_marked_resolved_from_the_page(demo):
    app, db = demo
    _, page = get(app, "/api/incidents?range=7d")
    escalated = next((r["id"] for r in page["rows"] if r["status"] == "escalated"), None)
    resolved = next(r["id"] for r in page["rows"] if r["status"] == "resolved")
    # a resolved incident has nothing to close
    status, _ = post(app, f"/api/incidents/{resolved}/decision", {"action": "acknowledge"})
    assert status == 409
    if escalated is None:
        return
    _, detail = get(app, f"/api/incidents/{escalated}")
    assert detail["can_resolve"] is True
    status, body = post(
        app, f"/api/incidents/{escalated}/decision", {"action": "acknowledge", "reason": "done"}
    )
    assert status == 200 and "resolution" in body["message"]
    waiting = StateStore(db).waiting_decisions()
    assert (waiting[0].action, waiting[0].reason) == ("acknowledge", "done")


def test_recovered_incidents_are_labelled_and_not_counted_as_healed(tmp_path):
    from healops.dashboard.views import CLOSED, handled_by, state_label
    from healops.store import RECOVERED

    assert RECOVERED in CLOSED
    db, audit = build_demo(tmp_path / "r", now=NOW)
    store = StateStore(db)
    stored = store.incidents()[0]
    store.save_incident(stored.incident, RECOVERED, "recovered on its own", now=NOW)
    app = DashboardApp(db, audit, views.Settings(), clock=lambda: NOW)
    snap = app._snapshot()
    again = next(s for s in snap.incidents if s.incident.incident_id == stored.incident.incident_id)
    assert state_label(snap, again) == ("Recovered", "idle")
    assert handled_by(snap, again) == "self"
    app.close()


# ---------- drift/signals, applications and agent pages ----------


def test_signals_page_has_checks_heatmap_and_charts(demo):
    app, _ = demo
    status, page = get(app, "/api/signals?range=7d")
    assert status == 200
    assert page["range"] == "7d" and page["app"] is None
    assert [c["label"] for c in page["checks"]] == ["Schema", "Freshness", "Missing symbols", "Values out of range"]
    by = {c["label"]: c for c in page["checks"]}
    assert by["Schema"]["tag"] == "PASS" and by["Schema"]["value"] == "Matches"
    assert by["Missing symbols"]["value"] == "2 of 147" and by["Missing symbols"]["tag"] == "WARN"
    assert by["Freshness"]["value"] == "Today"

    heat = page["heatmap"]
    assert len(heat["days"]) == views.HEATMAP_DAYS and heat["limit"] == 0.25
    assert heat["rows"] and all(len(r["cells"]) == views.HEATMAP_DAYS for r in heat["rows"])
    ranker = {r["name"]: r for r in heat["rows"] if r["app"] == "stock-ranker"}
    assert "prev_return_5d" in ranker
    assert ranker["prev_return_5d"]["latest"] > 0.25
    cells = [c for c in ranker["prev_return_5d"]["cells"] if c is not None]
    assert cells == sorted(cells)  # rising over the 14 days
    assert not any(r["name"].startswith("data.") for r in heat["rows"])

    for item in page["service"]:
        assert item["points"] and len(item["points"]) <= views.SIGNAL_POINTS
        assert item["min"] <= item["avg"] <= item["max"]
        assert not item["name"].startswith("data.") and item["kind"] != "drift"
    assert page["related_incident"] is not None


def test_signals_page_can_be_limited_to_one_application(demo):
    app, _ = demo
    _, page = get(app, "/api/signals?range=7d&app=market-direction")
    assert page["app"] == "market-direction"
    assert all(r["app"] == "market-direction" for r in page["heatmap"]["rows"])
    assert all(s["app"] == "market-direction" for s in page["service"])
    assert {c["tag"] for c in page["checks"]} == {"NO DATA"}  # that application sends no checks
    _, unknown = get(app, "/api/signals?app=no-such-app")
    assert unknown["app"] is None


def test_signals_history_is_not_cut_by_the_reading_limit(tmp_path, monkeypatch):
    """The page asks the database for slices, so 14 days show even with a tiny reading cap."""
    db, audit = build_demo(tmp_path / "cap", now=NOW)
    monkeypatch.setattr(views, "MAX_OBSERVATIONS", 50)
    app = DashboardApp(db, audit, views.Settings(drift_threshold=0.25), clock=lambda: NOW)
    _, page = get(app, "/api/signals?range=7d")
    ranker = next(r for r in page["heatmap"]["rows"] if r["name"] == "prev_return_5d" and r["app"] == "stock-ranker")
    assert sum(c is not None for c in ranker["cells"]) >= 13
    app.close()


def test_check_tiles_without_data_say_so():
    tiles = views._check_tiles({})
    assert all(t["tag"] == "NO DATA" and t["value"] == "Not reported" for t in tiles)
    tiles = views._check_tiles(
        {"data.schema_ok": 0, "data.freshness_days": 6, "data.missing_symbols": 30, "data.symbols_total": 147, "data.out_of_range": 3}
    )
    assert [t["tag"] for t in tiles] == ["FAIL", "WARN", "FAIL", "WARN"]
    assert tiles[1]["value"] == "6 days old"


def test_downsampling_keeps_the_worst_reading():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [(start + timedelta(minutes=i), 1.0) for i in range(1000)]
    points[500] = (points[500][0], 9.0)
    spiky = views._downsample(points, 50, worst_low=False)
    assert len(spiky) == 50 and max(p["value"] for p in spiky) == 9.0
    points = [(start + timedelta(minutes=i), 1.0) for i in range(1000)]
    points[300] = (points[300][0], 0.0)
    dips = views._downsample(points, 50, worst_low=True)
    assert min(p["value"] for p in dips) == 0.0


def test_apps_page_counts_incidents_by_who_closed_them(demo):
    app, _ = demo
    status, page = get(app, "/api/apps")
    assert status == 200 and page["apps"]
    _, incidents = get(app, "/api/incidents?range=30d")
    for row in page["apps"]:
        mine = [r for r in incidents["rows"] if r["app"] == row["app"]]
        assert row["incidents"]["total"] == len(mine)
        parts = row["incidents"]
        assert parts["open"] + parts["by_agent"] + parts["by_human"] + parts["by_self"] <= parts["total"]
        assert row["status"] in {"Healthy", "Healing", "Drifting", "Degraded"}


def test_agent_page_lists_setup_policies_and_searchable_audit(tmp_path):
    db, audit = build_demo(tmp_path / "a", now=NOW)
    settings = views.Settings(
        demo=True,
        agent={"mode": "Dry run", "recovery_checks": 3},
        policies=[{"action": "restart_component", "verdict": "allow", "max_retries": 1, "cooldown_seconds": 300}],
    )
    app = DashboardApp(db, audit, settings, clock=lambda: NOW)
    status, page = get(app, "/api/agent")
    assert status == 200
    assert page["setup"]["mode"] == "Dry run"
    assert page["policies"][0]["action"] == "restart_component"
    assert page["total"] >= len(page["events"]) > 0
    times = [e["at"] for e in page["events"]]
    assert times == sorted(times, reverse=True)

    _, none = get(app, "/api/agent?q=zzz-no-such-text")
    assert none["events"] == [] and none["matched"] == 0 and none["total"] == page["total"]
    first = page["events"][0]
    _, some = get(app, "/api/agent?q=" + first["incident"])
    assert some["matched"] >= 1 and all(first["incident"].lower() in (e["incident"] + e["text"] + e["app"] + e["stage"]).lower() for e in some["events"])
    app.close()


def test_agent_page_needs_no_config(demo):
    app, _ = demo
    _, page = get(app, "/api/agent")
    assert page["setup"] == {} and page["policies"] == []


def test_dashboard_cli_reads_agent_setup_from_the_config(tmp_path):
    import argparse

    from healops.dashboard.cli import _settings
    from healops.loader import load_config

    cfg = tmp_path / "c.yaml"
    cfg.write_text("dry_run: true\nrecovery_checks: 4\npolicies:\n  restart_component:\n    verdict: allow\n", encoding="utf-8")
    args = argparse.Namespace(name="X", env="local", by="ali", allow_actions=False)
    settings = _settings(args, load_config(cfg), False)
    assert settings.agent["mode"] == "Dry run" and settings.agent["recovery_checks"] == 4
    assert settings.agent["diagnosis"] == "rules only"
    actions = [p["action"] for p in settings.policies]
    assert actions[0] == "any other action" and "restart_component" in actions
    assert "api_key" not in str(settings.agent)


# ---------- 0.3.3: models, agent and integrations pages ----------


def test_apps_page_reports_last_retrain_changes_and_gates(demo):
    app, _ = demo
    _, page = get(app, "/api/apps")
    cement = next(a for a in page["apps"] if a["app"] == "sector-cement")
    assert cement["last_retrain"] is not None
    assert cement["changes"] and "sector-cement" in cement["changes"][0]["text"]
    quiet = next(a for a in page["apps"] if a["app"] == "sector-autos")
    assert quiet["last_retrain"] is None and quiet["changes"] == []
    assert page["gates"] == []  # the fixture gives no policies


def test_release_gates_follow_the_retrain_policy():
    settings = views.Settings(
        dry_run=False,
        policies=[
            {"action": "any other action", "verdict": "allow", "max_retries": 1, "cooldown_seconds": 0},
            {"action": "request_retraining", "verdict": "needs_approval", "max_retries": 2, "cooldown_seconds": 7 * 86400},
        ],
    )
    gates = views._release_gates(settings)
    assert gates[0] == {"label": "A person approves every retrain", "tag": "REQUIRED", "tone": "ok"}
    assert "At most 2 tries" in gates[1]["label"]
    assert gates[2]["label"] == "Wait 7 days between tries"
    assert views._release_gates(views.Settings()) == []


def test_agent_page_lists_what_waits_for_a_person(demo):
    app, _ = demo
    _, page = get(app, "/api/agent")
    assert len(page["waiting"]) == 1
    assert page["waiting"][0]["severity"] == "critical"
    assert page["audit_ok"] is True


def test_integrations_page_says_what_is_connected(demo):
    app, _ = demo
    _, page = get(app, "/api/integrations")
    assert page["adapter"]["status"] == "connected"
    assert len(page["adapter"]["apps"]) == 12
    assert page["email"] is None  # no config file, so unknown
    assert page["actions_enabled"] is True
    assert page["user"] == "tester"


def test_feature_psi_metrics_fill_the_heat_map_without_a_chart(demo):
    from healops.events import Observation
    from healops.types import ObservationKind

    app, db = demo
    store = StateStore(db)
    store.add_observations(
        [
            Observation("stock-ranker", ObservationKind.METRIC, "feature_psi.ret_5", 0.12, NOW - timedelta(days=2)),
            Observation("stock-ranker", ObservationKind.METRIC, "feature_psi.ret_5", 0.31, NOW - timedelta(hours=1)),
        ]
    )
    store.close()
    _, page = get(app, "/api/signals?range=7d&app=stock-ranker")
    row = next(r for r in page["heatmap"]["rows"] if r["name"] == "ret_5")
    assert row["latest"] == 0.31
    assert [c for c in row["cells"] if c is not None] == [0.12, 0.31]
    assert not any(s["name"].startswith("feature_psi.") for s in page["service"])


def test_token_is_required_when_set(tmp_path):
    db, audit = build_demo(tmp_path / "t", now=NOW)
    settings = views.Settings(actions_enabled=True, action_token="s3cret")
    app = DashboardApp(db, audit, settings, clock=lambda: NOW)
    path = f"/api/incidents/{ranker_id(app)}/decision"
    assert get(app, "/api/overview")[1]["shell"]["needs_token"] is True
    assert post(app, path, {"action": "approve"})[0] == 401
    assert post(app, path, {"action": "approve"}, {"X-HealOps-Token": "wrong"})[0] == 401
    assert StateStore(db).waiting_decisions() == []
    assert post(app, path, {"action": "approve"}, {"X-HealOps-Token": "s3cret"})[0] == 200
    app.close()


def test_actions_on_a_network_address_need_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALOPS_ACTION_TOKEN", raising=False)
    db = tmp_path / "s.db"
    StateStore(db).close()
    lines: list[str] = []
    args = ["dashboard", "--db", str(db), "--allow-actions", "--host", "0.0.0.0", "--port", "0"]
    assert cli_main(args, out=lines.append) == 2 and "access token" in " ".join(lines)
