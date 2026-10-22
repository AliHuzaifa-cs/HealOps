"""The public HealOps entry point, driven against the toy application."""

import pytest

import healops
from healops import HealOps, SelfHealingAgent
from healops.audit import read_events
from healops.config import HealOpsConfig
from healops.healing.notify import RecordingNotifier
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, Fault, ToyApp
from healops.types import IncidentState


def make(tmp_path=None, **overrides):
    data = {**DEFAULT_CONFIG, "dry_run": False, "audit_log_path": None, **overrides}
    alerts = RecordingNotifier()
    system = HealOps(data, notifiers=[alerts], sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    return system, app, alerts


def run_until_quiet(system, ticks=8):
    for _ in range(ticks):
        system.tick()


def test_the_entry_point_is_exported_with_an_alias():
    assert healops.HealOps is HealOps
    assert SelfHealingAgent is HealOps


def test_a_healthy_app_produces_no_incidents():
    system, _app, alerts = make()
    run_until_quiet(system)
    assert system.stats.incidents == 0
    assert alerts.alerts == []


def test_a_crash_is_detected_and_healed_end_to_end():
    system, app, _alerts = make()
    app.inject(Fault.CRASH)
    run_until_quiet(system)
    assert system.stats.resolved == 1
    assert system.stats.escalated == 0
    assert app.get_health() is True


def test_dry_run_is_the_default_and_changes_nothing():
    data = {**DEFAULT_CONFIG, "audit_log_path": None}
    data.pop("dry_run", None)
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    assert system.config.dry_run is True
    app.inject(Fault.CRASH)
    run_until_quiet(system)
    assert app.get_health() is False
    assert app.actions == []


def test_registering_a_restartable_app_requires_a_component():
    system = HealOps({**DEFAULT_CONFIG, "audit_log_path": None})
    with pytest.raises(ValueError, match="component"):
        system.register("toy", ToyApp())


def test_a_blank_component_is_rejected():
    system = HealOps({**DEFAULT_CONFIG, "audit_log_path": None})
    with pytest.raises(ValueError):
        system.register("toy", ToyApp(), component="  ")


def test_running_without_any_application_is_an_error():
    system = HealOps({**DEFAULT_CONFIG, "audit_log_path": None})
    with pytest.raises(ValueError, match="register"):
        system.run(max_ticks=1)
    with pytest.raises(ValueError, match="register"):
        system.tick()


def test_each_app_restarts_its_own_component():
    system, app, _ = make()
    other = ToyApp()
    system.register("other", other, component="other-component")
    app.inject(Fault.CRASH)
    run_until_quiet(system)
    assert app.actions == [f"restart_component:{COMPONENT}"]
    assert other.actions == []


def test_the_audit_log_is_written_to_the_configured_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    data = {**DEFAULT_CONFIG, "dry_run": False, "audit_log_path": str(path)}
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    run_until_quiet(system)
    assert len(read_events(path)) > 0


def test_a_config_file_path_is_accepted(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("dry_run: false\naudit_log_path: null\nthresholds:\n  latency_ms: 500\n")
    system = HealOps.from_config(path)
    assert system.config.thresholds.latency_ms == 500


def test_an_approval_can_be_granted_through_the_entry_point():
    policies = {"restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0}}
    system, app, _ = make(policies={**DEFAULT_CONFIG["policies"], **policies})
    app.inject(Fault.CRASH)
    run_until_quiet(system, ticks=4)
    waiting = system.pending_approvals()
    assert len(waiting) == 1
    assert app.get_health() is False
    result = system.approve(
        waiting[0].incident.incident_id, waiting[0].proposal.proposal_id, "ali"
    )
    assert result.incident.state is IncidentState.RESOLVED
    assert app.get_health() is True
    assert system.pending_approvals() == ()


def test_a_rejected_approval_escalates_and_changes_nothing():
    policies = {"restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0}}
    system, app, _ = make(policies={**DEFAULT_CONFIG["policies"], **policies})
    app.inject(Fault.CRASH)
    run_until_quiet(system, ticks=4)
    waiting = system.pending_approvals()[0]
    result = system.reject(waiting.incident.incident_id, waiting.proposal.proposal_id, "ali", "no")
    assert result.incident.state is IncidentState.ESCALATED
    assert app.get_health() is False
    assert waiting.incident.incident_id in system.incidents()


def test_the_config_is_validated():
    with pytest.raises(Exception):  # noqa: B017
        HealOps({"nonsense": 1})


def test_audit_path_null_keeps_the_log_in_memory():
    config = HealOpsConfig.model_validate({"audit_log_path": None})
    assert config.audit_log_path is None
    assert HealOpsConfig().audit_log_path == "healops_audit.jsonl"


def test_a_blank_audit_path_is_rejected():
    with pytest.raises(ValueError):
        HealOpsConfig.model_validate({"audit_log_path": " "})


def test_agents_resolve_the_component_per_application():
    from healops.agents.base import resolve_component
    from healops.agents.mock import MockDiagnosisAgent

    assert resolve_component("x", "any") == "x"
    assert resolve_component({"a": "ca"}, "a") == "ca"
    assert resolve_component({"a": "ca"}, "b") == "application"
    with pytest.raises(ValueError):
        MockDiagnosisAgent(component={"a": " "})
    with pytest.raises(TypeError):
        MockDiagnosisAgent(component=5)  # type: ignore[arg-type]