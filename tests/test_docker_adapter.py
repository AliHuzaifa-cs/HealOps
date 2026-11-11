"""Docker restarts, the playbook guard, restart targets and the per-action confidence."""

import json
from datetime import UTC, datetime

import pytest

from healops import HealOps
from healops.adapters.docker import DockerError, DockerRestarter, DockerServiceAdapter
from healops.agents.base import AgentResult, DiagnosisContext
from healops.agents.factory import build_agent, build_playbook
from healops.agents.fallback import FallbackAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.agents.playbook import OutsidePlaybook, PlaybookGuard, TargetRouter
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.healing.notify import RecordingNotifier
from healops.healing.policy import PolicyEngine
from healops.records import ActionProposal, Diagnosis
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class FakeEngine:
    def __init__(self, containers=None, restart_status=204):
        self.containers = [{"Id": "abc123"}] if containers is None else containers
        self.restart_status = restart_status
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        if method == "GET":
            return 200, json.dumps(self.containers).encode()
        return self.restart_status, b""


def incident(kind=IncidentType.HIGH_ERROR_RATE):
    evidence = Observation(
        app_name="app", kind=ObservationKind.METRIC, name="error_rate", value=1.0, timestamp=NOW
    )
    return Incident(
        app_name="app", incident_type=kind, severity=Severity.HIGH, summary="s", evidence=(evidence,),
    )


def context(kind=IncidentType.HIGH_ERROR_RATE):
    return DiagnosisContext(
        incident=incident(kind),
        available_actions=frozenset(ActionType),
    )


class Says:
    """A stand-in model agent that always proposes one action."""

    def __init__(self, action, confidence=0.8):
        self.action, self.confidence = action, confidence

    def diagnose(self, ctx):
        inc = ctx.incident
        diagnosis = Diagnosis(incident_id=inc.incident_id, summary="m", confidence=self.confidence,
                              probable_cause="c", evidence_used=("error_rate",))
        proposal = ActionProposal(incident_id=inc.incident_id, diagnosis_id=diagnosis.diagnosis_id,
                                  action_type=self.action, rationale="r", expected_outcome="e",
                                  params={"component": "x"} if self.action is ActionType.RESTART_COMPONENT else {})
        return AgentResult(diagnosis, proposal)


class Broken:
    def diagnose(self, ctx):
        raise RuntimeError("model is down")


# ---- the Docker restarter -------------------------------------------------------------
def test_it_looks_the_service_up_by_its_label_and_restarts_that_container():
    engine = FakeEngine()
    message = DockerRestarter(["api"], request=engine).restart("api")
    assert "api" in message
    assert engine.calls[0][0] == "GET" and "com.docker.compose.service" in engine.calls[0][1].replace("%3D", "=").replace("%22", '"')
    assert engine.calls[1] == ("POST", "/containers/abc123/restart?t=10")


def test_a_service_that_is_not_listed_is_never_touched():
    engine = FakeEngine()
    with pytest.raises(DockerError):
        DockerRestarter(["api"], request=engine).restart("monitor")
    assert engine.calls == []


def test_failures_do_not_leak_details():
    with pytest.raises(DockerError, match="no container found"):
        DockerRestarter(["api"], request=FakeEngine(containers=[])).restart("api")
    with pytest.raises(DockerError, match="did not restart"):
        DockerRestarter(["api"], request=FakeEngine(restart_status=500)).restart("api")

    def broken(method, path):
        raise ConnectionRefusedError("secret path /var/run/docker.sock")

    with pytest.raises(DockerError) as caught:
        DockerRestarter(["api"], request=broken).restart("api")
    assert "secret" not in str(caught.value)


def test_the_wrapper_sends_allowed_services_to_docker_and_the_rest_to_the_adapter():
    class Inner:
        def get_health(self):
            return True

        def restart_component(self, component):
            return f"inner restarted {component}"

        def reload_model(self):
            return "reloaded"

    engine = FakeEngine()
    wrapper = DockerServiceAdapter(Inner(), DockerRestarter(["api"], request=engine))
    assert "api container" in wrapper.restart_component("api")
    assert wrapper.last_action == "restart_component"
    assert wrapper.restart_component("job") == "inner restarted job"
    assert wrapper.reload_model() == "reloaded" and wrapper.last_action == "reload_model"
    assert wrapper.get_health() is True


def test_the_wrapper_cannot_restart_other_components_when_the_adapter_cannot():
    class Inner:
        def get_health(self):
            return True

    wrapper = DockerServiceAdapter(Inner(), DockerRestarter(["api"], request=FakeEngine()))
    with pytest.raises(DockerError):
        wrapper.restart_component("job")


# ---- config ---------------------------------------------------------------------------
def test_config_accepts_the_new_sections_and_rejects_bad_ones():
    config = HealOpsConfig.model_validate({
        "docker": {"restartable_services": ["api"]},
        "playbook": {"high_error_rate": ["restart_component", "run_diagnostic", "escalate"]},
        "restart_targets": {"high_error_rate": "api"},
        "policies": {"restart_component": {"verdict": "allow", "min_confidence": 0.1}},
    })
    assert config.docker.restartable_services == ["api"]
    for bad in (
        {"docker": {"restartable_services": ["api; rm -rf /"]}},
        {"docker": {"restartable_services": []}},
        {"playbook": {"high_error_rate": []}},
        {"playbook": {"high_error_rate": ["no_action"]}},
        {"restart_targets": {"high_error_rate": "a b"}},
    ):
        with pytest.raises(ValueError):
            HealOpsConfig.model_validate(bad)


def test_the_configured_playbook_replaces_only_its_incident_type():
    config = HealOpsConfig.model_validate(
        {"playbook": {"high_error_rate": ["restart_component", "escalate"]}}
    )
    merged = build_playbook(config)
    assert merged[IncidentType.HIGH_ERROR_RATE] == (ActionType.RESTART_COMPONENT, ActionType.ESCALATE)
    assert merged[IncidentType.DATA_DRIFT] == build_playbook(HealOpsConfig())[IncidentType.DATA_DRIFT]


# ---- agents ---------------------------------------------------------------------------
def test_the_guard_refuses_an_action_outside_the_playbook_but_allows_escalation():
    playbook = {IncidentType.HIGH_ERROR_RATE: (ActionType.RESTART_COMPONENT, ActionType.ESCALATE)}
    with pytest.raises(OutsidePlaybook):
        PlaybookGuard(Says(ActionType.REQUEST_RETRAINING), playbook).diagnose(context())
    PlaybookGuard(Says(ActionType.RESTART_COMPONENT), playbook).diagnose(context())
    PlaybookGuard(Says(ActionType.ESCALATE), playbook).diagnose(context())


def test_a_model_that_answers_outside_the_playbook_is_replaced_by_the_rules():
    config = HealOpsConfig.model_validate({
        "playbook": {"high_error_rate": ["restart_component", "escalate"]},
        "restart_targets": {"high_error_rate": "api"},
    })
    guard = PlaybookGuard(Says(ActionType.REQUEST_RETRAINING), build_playbook(config))
    agent = TargetRouter(
        FallbackAgent(guard, MockDiagnosisAgent(playbook=build_playbook(config))),
        config.restart_targets,
    )
    result = agent.diagnose(context())
    assert result.proposal.action_type is ActionType.RESTART_COMPONENT
    assert result.proposal.params["component"] == "api"
    assert result.diagnosis.confidence <= 0.2 and "FALLBACK" in result.diagnosis.summary


def test_restart_targets_apply_only_to_their_incident_types():
    router = TargetRouter(Says(ActionType.RESTART_COMPONENT), {IncidentType.HIGH_LATENCY: "api"})
    assert router.diagnose(context(IncidentType.HIGH_LATENCY)).proposal.params["component"] == "api"
    assert router.diagnose(context(IncidentType.HEALTH_FAILURE)).proposal.params["component"] == "x"


def test_build_agent_without_new_sections_is_the_rule_agent():
    assert isinstance(build_agent(HealOpsConfig()), MockDiagnosisAgent)


# ---- policy ---------------------------------------------------------------------------
def test_an_action_can_have_its_own_confidence_limit():
    config = HealOpsConfig.model_validate({
        "min_confidence_for_auto": 0.6,
        "policies": {"restart_component": {"verdict": "allow", "min_confidence": 0.1}},
    })
    engine = PolicyEngine(config)
    result = Says(ActionType.RESTART_COMPONENT, confidence=0.2).diagnose(context())
    decision = engine.evaluate(result.proposal, result.diagnosis, frozenset(ActionType))
    assert decision.verdict is PolicyVerdict.ALLOW
    # the global limit still holds for an action without its own
    other = HealOpsConfig.model_validate({
        "min_confidence_for_auto": 0.6, "policies": {"restart_component": {"verdict": "allow"}},
    })
    decision = PolicyEngine(other).evaluate(result.proposal, result.diagnosis, frozenset(ActionType))
    assert decision.verdict is PolicyVerdict.NEEDS_APPROVAL


# ---- the whole loop through the facade --------------------------------------------------
class ApiApp:
    """The thing being watched: it fails until the 'api' service is restarted."""

    def __init__(self):
        self.up = False

    def get_health(self):
        return True

    def get_metrics(self):
        return {"error_rate": 0.0 if self.up else 1.0}

    def restart_component(self, component):
        raise AssertionError("the adapter must not be asked to restart a docker service")


def test_an_api_outage_is_healed_by_a_docker_restart_without_a_person():
    app = ApiApp()

    def engine(method, path):
        if method == "GET":
            return 200, b'[{"Id": "abc"}]'
        app.up = True  # the restart brings the service back
        return 204, b""

    config = {
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": None,
        "min_confidence_for_auto": 0.6,
        "thresholds": {"error_rate": 0.3, "consecutive_breaches": 2},
        "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0, "max_window_seconds": 10},
        "docker": {"restartable_services": ["api"]},
        "playbook": {"high_error_rate": ["restart_component", "run_diagnostic", "escalate"]},
        "restart_targets": {"high_error_rate": "api"},
        "policies": {"restart_component": {"verdict": "allow", "min_confidence": 0.1, "cooldown_seconds": 0}},
    }
    system = HealOps(config, notifiers=[RecordingNotifier()], sleep=lambda _s: None, docker_request=engine)
    system.register("app", app, component="app-job")
    for _ in range(6):
        system.tick()
    assert app.up is True
    assert system.stats.resolved == 1 and system.stats.escalated == 0


def test_without_a_docker_section_nothing_is_wrapped():
    system = HealOps({"audit_log_path": None, "state_db_path": None}, notifiers=[RecordingNotifier()])

    class Plain:
        def get_health(self):
            return True

    registered = system.register("p", Plain(), guard=False)
    assert isinstance(registered.adapter, Plain)
