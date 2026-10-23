"""Time limits for adapter calls: a hanging application must not hang HealOps."""

import inspect
import threading
import time

import pytest

from healops import HealOps, interfaces
from healops.capabilities import Capabilities
from healops.config import AdapterTimeoutsConfig, HealOpsConfig
from healops.exceptions import AdapterTimeout, AdapterUnresponsive
from healops.guard import ACTION_METHODS, ALL_METHODS, READ_METHODS, GuardedAdapter
from healops.healing.notify import RecordingNotifier
from healops.interfaces import ComponentRestarter, HealthProvider
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, Fault, ToyApp


class Hanging:
    """An adapter whose methods block until released."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = []

    def get_health(self):
        self.calls.append("get_health")
        self.release.wait(10)
        return True

    def get_metrics(self):
        self.calls.append("get_metrics")
        self.release.wait(10)
        return {"latency_ms": 1.0}

    def restart_component(self, component):
        self.calls.append(f"restart:{component}")
        self.release.wait(10)
        return "restarted"


@pytest.fixture
def hanging():
    adapter = Hanging()
    yield adapter
    adapter.release.set()


def guard(adapter, read=0.1, action=0.3, stuck=3):
    return GuardedAdapter(adapter, "app", read, action, stuck)


class Plain:
    def get_health(self):
        return True

    def get_metrics(self):
        return {"latency_ms": 5.0}

    def restart_component(self, component, force=False):
        return f"restarted {component} force={force}"

    def explode(self):
        raise RuntimeError("boom")

    def get_drift_scores(self):
        raise ValueError("bad")


def test_fast_calls_pass_through_with_arguments_and_results():
    g = guard(Plain())
    assert g.get_health() is True
    assert g.get_metrics() == {"latency_ms": 5.0}
    assert g.restart_component("x", force=True) == "restarted x force=True"


def test_errors_from_the_adapter_keep_their_type():
    with pytest.raises(ValueError, match="bad"):
        guard(Plain()).get_drift_scores()


def test_even_a_keyboard_interrupt_reaches_the_caller():
    class Dying:
        def reload_model(self):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        guard(Dying()).reload_model()


def test_a_slow_read_times_out_without_blocking_the_caller(hanging):
    g = guard(hanging, read=0.1)
    started = time.monotonic()
    with pytest.raises(AdapterTimeout, match="get_health on app timed out after 0.1s") as caught:
        g.get_health()
    assert time.monotonic() - started < 2
    assert "outcome is unknown" not in str(caught.value)


def test_a_slow_action_says_its_outcome_is_unknown(hanging):
    with pytest.raises(AdapterTimeout, match="outcome is unknown"):
        guard(hanging, action=0.1).restart_component("c")


def test_reads_and_actions_have_their_own_limits():
    class Slow:
        def get_health(self):
            time.sleep(0.25)
            return True

        def reload_model(self):
            time.sleep(0.25)
            return "ok"

    g = guard(Slow(), read=0.05, action=1.0)
    with pytest.raises(AdapterTimeout):
        g.get_health()
    assert g.reload_model() == "ok"


def test_stuck_calls_are_counted_and_then_no_new_call_is_made(hanging):
    g = guard(hanging, read=0.05, stuck=2)
    for _ in range(2):
        with pytest.raises(AdapterTimeout):
            g.get_health()
    assert g.stuck_calls == 2
    before = list(hanging.calls)
    started = time.monotonic()
    with pytest.raises(AdapterUnresponsive, match="still stuck"):
        g.get_metrics()
    assert time.monotonic() - started < 0.04
    assert hanging.calls == before  # the adapter was not called again


def test_the_adapter_is_used_again_once_the_stuck_calls_finish(hanging):
    g = guard(hanging, read=0.05, stuck=1)
    with pytest.raises(AdapterTimeout):
        g.get_health()
    with pytest.raises(AdapterUnresponsive):
        g.get_health()
    hanging.release.set()
    deadline = time.monotonic() + 3
    while g.stuck_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert g.stuck_calls == 0
    assert g.get_health() is True


def test_only_the_methods_an_adapter_has_are_wrapped():
    class OnlyHealth:
        def get_health(self):
            return True

    g = guard(OnlyHealth())
    assert isinstance(g, HealthProvider)
    assert not isinstance(g, ComponentRestarter)
    assert Capabilities.discover(g).names() == Capabilities.discover(OnlyHealth()).names()
    assert g.wrapped.__class__ is OnlyHealth


def test_every_capability_method_is_covered():
    found = set()
    for _name, cls in inspect.getmembers(interfaces, inspect.isclass):
        if getattr(cls, "_is_protocol", False):
            found |= {n for n, v in vars(cls).items() if callable(v) and not n.startswith("_")}
    found.discard("__init__")
    assert found == set(ALL_METHODS)
    assert READ_METHODS.isdisjoint(ACTION_METHODS)


@pytest.mark.parametrize("args", [(0, 1, 1), (1, 0, 1), (1, 1, 0)])
def test_bad_limits_are_refused(args):
    with pytest.raises(ValueError):
        GuardedAdapter(Plain(), "a", *args)


def test_the_config_has_safe_defaults_and_bounds():
    config = HealOpsConfig()
    assert config.adapter_timeouts == AdapterTimeoutsConfig(
        read_seconds=10.0, action_seconds=120.0, max_stuck_calls=3
    )
    for bad in ({"read_seconds": 0}, {"action_seconds": -1}, {"max_stuck_calls": 0}, {"x": 1}):
        with pytest.raises(ValueError):
            HealOpsConfig.model_validate({"adapter_timeouts": bad})


# ----- through HealOps -----


def make(policies=None, **timeouts):
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": None,
        "adapter_timeouts": {
            "read_seconds": 0.1,
            "action_seconds": 0.3,
            "max_stuck_calls": 3,
            **timeouts,
        },
    }
    if policies:
        data["policies"] = {**DEFAULT_CONFIG["policies"], **policies}
    alerts = RecordingNotifier()
    return HealOps(data, notifiers=[alerts], sleep=lambda _s: None), alerts


def test_a_hanging_application_does_not_stop_the_others(hanging):
    system, _ = make()
    healthy = ToyApp()
    system.register("slow", hanging, component="c")
    system.register("toy", healthy, component=COMPONENT)
    healthy.inject(Fault.CRASH)
    started = time.monotonic()
    for _ in range(4):
        system.tick()
    assert time.monotonic() - started < 5
    assert healthy.get_health() is True
    assert system.stuck_calls()["slow"] >= 1


def test_a_hanging_health_check_counts_as_unhealthy(hanging):
    system, _ = make()
    system.register("slow", hanging, component="c")
    reports = [system.tick() for _ in range(3)]
    assert any(r.detected for r in reports)
    assert any("AdapterTimeout" in e or "AdapterUnresponsive" in e for r in reports for e in r.errors)


def test_an_action_that_times_out_is_escalated_and_not_retried():
    class StuckRestart(ToyApp):
        calls = 0
        release = threading.Event()

        def restart_component(self, component):
            StuckRestart.calls += 1
            self.release.wait(10)
            return "late"

    StuckRestart.release = threading.Event()
    system, alerts = make()
    app = StuckRestart()
    system.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    try:
        for _ in range(6):
            system.tick()
        assert StuckRestart.calls == 1
        assert system.stats.escalated == 1
        assert system.stats.resolved == 0
        [result] = system.incidents().values()
        assert result.state.value == "escalated"
        assert any("needs a person" in a.title for a in alerts.alerts)
    finally:
        StuckRestart.release.set()


def test_guard_can_be_switched_off_per_application():
    system, _ = make()
    app = ToyApp()
    application = system.register("toy", app, component=COMPONENT, guard=False)
    assert application.adapter is app
    assert system.stuck_calls() == {}


def test_the_guarded_adapter_is_what_gets_registered_by_default():
    system, _ = make()
    app = ToyApp()
    application = system.register("toy", app, component=COMPONENT)
    assert isinstance(application.adapter, GuardedAdapter)
    assert application.adapter.wrapped is app
    assert system.stuck_calls() == {"toy": 0}


def test_a_failed_registration_leaves_no_guard_behind():
    system, _ = make()
    system.register("toy", ToyApp(), component=COMPONENT)
    with pytest.raises(Exception):  # noqa: B017 - the registry refuses the duplicate name
        system.register("toy", ToyApp(), component=COMPONENT)
    assert list(system.stuck_calls()) == ["toy"]