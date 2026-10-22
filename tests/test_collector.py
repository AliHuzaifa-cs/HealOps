from datetime import UTC, datetime

from healops.collector import HEALTH_NAME, collect
from healops.registry import ApplicationRegistry
from healops.testing import Fault, ToyApp
from healops.types import ObservationKind

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def clock():
    return NOW


def register(adapter, name="demo-app"):
    return ApplicationRegistry().register(name, adapter)


def by_name(result):
    return {o.name: o for o in result.observations}


def test_a_healthy_toy_app_gives_health_metrics_and_drift():
    result = collect(register(ToyApp()), clock)
    seen = by_name(result)
    assert set(seen) == {
        HEALTH_NAME,
        "latency_ms",
        "error_rate",
        "f1_score",
        "input_distribution",
    }
    assert seen[HEALTH_NAME].kind is ObservationKind.HEALTH
    assert seen[HEALTH_NAME].value is True
    assert seen["latency_ms"].kind is ObservationKind.METRIC
    assert seen["input_distribution"].kind is ObservationKind.DRIFT
    assert result.errors == ()


def test_every_observation_belongs_to_the_app_and_shares_one_timestamp():
    result = collect(register(ToyApp(), "my-app"), clock)
    assert {o.app_name for o in result.observations} == {"my-app"}
    assert {o.timestamp for o in result.observations} == {NOW}


def test_injected_faults_show_up_in_the_observations():
    app = ToyApp()
    app.inject(Fault.CRASH)
    app.inject(Fault.BAD_MODEL)
    seen = by_name(collect(register(app), clock))
    assert seen[HEALTH_NAME].value is False
    assert seen["error_rate"].value == 0.20
    assert seen["f1_score"].value == 0.62


def test_only_the_capabilities_an_app_has_are_read():
    class HealthOnly:
        def get_health(self):
            return True

    result = collect(register(HealthOnly()), clock)
    assert [o.name for o in result.observations] == [HEALTH_NAME]


def test_a_crashing_health_check_counts_as_unhealthy_not_healthy():
    class Broken:
        def get_health(self):
            raise ConnectionError("db password=hunter2")

        def get_metrics(self):
            return {"latency_ms": 100.0}

    result = collect(register(Broken()), clock)
    seen = by_name(result)
    assert seen[HEALTH_NAME].value is False
    assert seen[HEALTH_NAME].details == {"error": "ConnectionError"}
    assert seen["latency_ms"].value == 100.0
    assert "hunter2" not in " ".join(result.errors)
    assert "hunter2" not in str(seen[HEALTH_NAME].details)


def test_a_failing_metrics_read_is_an_error_and_never_an_invented_number():
    class BrokenMetrics:
        def get_health(self):
            return True

        def get_metrics(self):
            raise TimeoutError("slow")

    result = collect(register(BrokenMetrics()), clock)
    assert [o.name for o in result.observations] == [HEALTH_NAME]
    assert result.errors == ("get_metrics failed (TimeoutError)",)


def test_one_failing_provider_does_not_stop_the_others():
    class Mixed:
        def get_metrics(self):
            raise RuntimeError("x")

        def get_drift_scores(self):
            return {"feature": 0.1}

    result = collect(register(Mixed()), clock)
    assert [o.name for o in result.observations] == ["feature"]
    assert len(result.errors) == 1


def test_bad_readings_are_skipped_with_an_error_and_good_ones_kept():
    class Odd:
        def get_metrics(self):
            return {"ok": 1, "text": "high", "flag": True, "nan": float("nan"), "inf": float("inf")}

    result = collect(register(Odd()), clock)
    assert {o.name: o.value for o in result.observations} == {"ok": 1.0}
    assert len(result.errors) == 4


def test_an_app_with_empty_readings_gives_no_observations_and_no_errors():
    class Empty:
        def get_metrics(self):
            return {}

    result = collect(register(Empty()), clock)
    assert result.observations == ()
    assert result.errors == ()


def test_collecting_never_changes_the_application():
    app = ToyApp()
    app.inject(Fault.SLOW)
    collect(register(app), clock)
    assert app.faults == {Fault.SLOW}
    assert app.actions == []