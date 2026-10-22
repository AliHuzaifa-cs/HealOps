import threading

import pytest

from healops.agents.base import DiagnosisContext
from healops.agents.factory import build_agent
from healops.agents.fallback import FallbackAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.agents.ratelimit import RateLimitedAgent
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.exceptions import LLMError, RateLimitExceeded
from healops.healing.orchestrator import HandleStatus
from healops.loader import parse_config
from healops.testing import COMPONENT, Fault, make_toy_system
from healops.types import ActionType, IncidentType, ObservationKind, Severity


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Counting:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.inner = MockDiagnosisAgent()

    def diagnose(self, context):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        return self.inner.diagnose(context)


def context():
    obs = Observation(app_name="a", kind=ObservationKind.HEALTH, name="health", value=False)
    incident = Incident(
        app_name="a",
        incident_type=IncidentType.HEALTH_FAILURE,
        severity=Severity.CRITICAL,
        summary="health check failed",
        evidence=(obs,),
    )
    return DiagnosisContext(
        incident=incident,
        available_actions=frozenset({ActionType.RESTART_COMPONENT, ActionType.ESCALATE}),
    )


def test_calls_within_the_budget_go_through():
    inner, clock = Counting(), Clock()
    agent = RateLimitedAgent(inner, max_calls=3, per_seconds=60, monotonic=clock)
    for _ in range(3):
        agent.diagnose(context())
    assert inner.calls == 3
    assert agent.calls_used == 3
    assert agent.calls_left == 0


def test_once_the_budget_is_used_up_the_agent_is_not_called_at_all():
    inner, clock = Counting(), Clock()
    agent = RateLimitedAgent(inner, max_calls=2, per_seconds=60, monotonic=clock)
    agent.diagnose(context())
    agent.diagnose(context())
    with pytest.raises(RateLimitExceeded, match="2 per 60 seconds"):
        agent.diagnose(context())
    assert inner.calls == 2
    assert agent.refused_count == 1


def test_the_budget_comes_back_as_old_calls_leave_the_window():
    inner, clock = Counting(), Clock()
    agent = RateLimitedAgent(inner, max_calls=1, per_seconds=60, monotonic=clock)
    agent.diagnose(context())
    clock.now += 59
    with pytest.raises(RateLimitExceeded):
        agent.diagnose(context())
    clock.now += 1  # exactly 60 seconds after the first call
    agent.diagnose(context())
    assert inner.calls == 2


def test_failed_calls_use_up_the_budget_too():
    inner, clock = Counting(fail=True), Clock()
    agent = RateLimitedAgent(inner, max_calls=2, per_seconds=60, monotonic=clock)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            agent.diagnose(context())
    with pytest.raises(RateLimitExceeded):
        agent.diagnose(context())
    assert inner.calls == 2


def test_it_is_an_llm_error_so_a_fallback_catches_it():
    assert issubclass(RateLimitExceeded, LLMError)


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        RateLimitedAgent(Counting(), max_calls=0)
    with pytest.raises(ValueError):
        RateLimitedAgent(Counting(), max_calls=1, per_seconds=0)


def test_it_never_lets_more_calls_through_than_the_budget_even_across_threads():
    inner = Counting()
    agent = RateLimitedAgent(inner, max_calls=5, per_seconds=3600)
    refused = []

    def worker():
        for _ in range(10):
            try:
                agent.diagnose(context())
            except RateLimitExceeded:
                refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inner.calls == 5
    assert len(refused) == 75


def test_with_the_budget_gone_a_fallback_answers_and_says_why():
    clock = Clock()
    limited = RateLimitedAgent(Counting(), max_calls=1, per_seconds=60, monotonic=clock)
    agent = FallbackAgent(limited, MockDiagnosisAgent())
    agent.diagnose(context())
    result = agent.diagnose(context())
    assert result.diagnosis.summary.startswith(
        "[FALLBACK: primary agent unavailable (RateLimitExceeded)]"
    )
    assert result.diagnosis.confidence <= 0.2


# ---------- from configuration ----------


def test_the_default_budget_is_twenty_calls_an_hour():
    config = parse_config({"llm": {"provider": "ollama", "model": "m"}})
    assert config.llm.max_calls_per_hour == 20


@pytest.mark.parametrize("bad", [0, -1, 1.5, "many"])
def test_a_nonsense_budget_is_refused(bad):
    from healops.exceptions import ConfigError

    with pytest.raises(ConfigError):
        parse_config({"llm": {"provider": "ollama", "model": "m", "max_calls_per_hour": bad}})


def _config(**llm):
    return HealOpsConfig.model_validate({"llm": {"provider": "ollama", "model": "m", **llm}})


def test_the_budget_can_be_switched_off_with_null():
    sent = []

    def transport(request):
        sent.append(request)
        raise LLMError("could not reach the model (ConnectionRefusedError)")

    agent = build_agent(_config(max_calls_per_hour=None), transport=transport)
    for _ in range(30):
        agent.diagnose(context())
    assert len(sent) == 30


def test_a_configured_agent_stops_calling_the_model_when_the_budget_is_gone():
    sent = []

    def transport(request):
        sent.append(request)
        raise LLMError("could not reach the model (ConnectionRefusedError)")

    agent = build_agent(_config(max_calls_per_hour=2), component=COMPONENT, transport=transport)
    summaries = [agent.diagnose(context()).diagnosis.summary for _ in range(5)]
    assert len(sent) == 2  # only two calls reached the model server
    assert "(LLMError)" in summaries[0]
    assert "(RateLimitExceeded)" in summaries[2]
    assert all(s.startswith("[FALLBACK") for s in summaries)


def test_without_a_fallback_a_spent_budget_escalates_instead_of_crashing():
    config = HealOpsConfig.model_validate(
        {
            "thresholds": {"consecutive_breaches": 2},
            "llm": {
                "provider": "ollama",
                "model": "m",
                "max_calls_per_hour": 1,
                "use_fallback": False,
            },
        }
    )

    def transport(request):
        raise LLMError("could not reach the model (ConnectionRefusedError)")

    from healops.testing import detect, heal

    system = make_toy_system(config, agent=build_agent(config, transport=transport))
    system.app.inject(Fault.CRASH)
    first = heal(system, detect(system)[0])
    assert first.status is HandleStatus.ESCALATED
    system.detector.release(first.incident.incident_id)
    second = heal(system, detect(system)[0])
    assert second.status is HandleStatus.ESCALATED
    assert "RateLimitExceeded" in second.reason
    assert system.app.actions == []