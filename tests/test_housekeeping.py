"""Recovery attempts from config, bounded action history, and warnings for unreported metrics."""

from datetime import UTC, datetime, timedelta

import pytest

from healops import HealOps
from healops.config import HealOpsConfig
from healops.facade import HISTORY_MARGIN, HISTORY_MIN_RETENTION, _new_history
from healops.healing.history import ActionHistory
from healops.healing.notify import RecordingNotifier
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, ToyApp
from healops.types import ActionType

T0 = datetime(2026, 8, 1, tzinfo=UTC)


def make(adapter, component="c", **config):
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": None,
        "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
        **config,
    }
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    system.register("app", adapter, component=component)
    return system


# ----- max_recovery_attempts -----


class NeverRecovers:
    """Reports unhealthy forever, whatever HealOps does."""

    def __init__(self):
        self.actions = []

    def get_health(self):
        return False

    def restart_component(self, component):
        self.actions.append("restart")
        return "restarted"

    def reload_model(self):
        self.actions.append("reload")
        return "reloaded"


def run_until_settled(system, ticks=8):
    for _ in range(ticks):
        system.tick()


def test_recovery_attempts_come_from_the_config():
    one = NeverRecovers()
    run_until_settled(make(one, max_recovery_attempts=1))
    assert one.actions == ["restart"]

    three = NeverRecovers()
    run_until_settled(make(three, max_recovery_attempts=3))
    assert three.actions == ["restart", "reload"]


def test_recovery_attempts_default_and_bounds():
    assert HealOpsConfig().max_recovery_attempts == 3
    for bad in (0, 11, -1):
        with pytest.raises(ValueError):
            HealOpsConfig.model_validate({"max_recovery_attempts": bad})


# ----- action history -----


def test_old_records_are_dropped_relative_to_the_newest():
    history = ActionHistory(retention=timedelta(hours=1))
    history.record("a", "i1", ActionType.RESTART_COMPONENT, T0)
    history.record("a", "i2", ActionType.RESTART_COMPONENT, T0 + timedelta(minutes=30))
    assert len(history) == 2
    history.record("a", "i3", ActionType.RESTART_COMPONENT, T0 + timedelta(minutes=90))
    assert len(history) == 2
    assert history.attempts("i1", ActionType.RESTART_COMPONENT) == 0
    assert history.attempts("i2", ActionType.RESTART_COMPONENT) == 1


def test_a_record_inside_the_window_still_drives_the_cooldown():
    history = ActionHistory(retention=timedelta(hours=1))
    history.record("a", "i1", ActionType.RESTART_COMPONENT, T0)
    history.record("a", "i2", ActionType.RELOAD_MODEL, T0 + timedelta(minutes=59))
    assert history.last_executed("a", ActionType.RESTART_COMPONENT) == T0


def test_the_number_of_records_is_capped_keeping_the_newest():
    history = ActionHistory(max_records=3)
    for n in range(10):
        history.record("a", f"i{n}", ActionType.RESTART_COMPONENT, T0 + timedelta(seconds=n))
    assert len(history) == 3
    assert history.attempts("i9", ActionType.RESTART_COMPONENT) == 1
    assert history.attempts("i6", ActionType.RESTART_COMPONENT) == 0


def test_without_limits_everything_is_kept():
    history = ActionHistory()
    for n in range(50):
        history.record("a", f"i{n}", ActionType.RESTART_COMPONENT, T0 + timedelta(days=n))
    assert len(history) == 50


@pytest.mark.parametrize("kwargs", [{"retention": timedelta(0)}, {"max_records": 0}])
def test_bad_history_limits_are_refused(kwargs):
    with pytest.raises(ValueError):
        ActionHistory(**kwargs)


def test_the_retention_covers_every_cooldown_and_approval_window():
    short = HealOpsConfig.model_validate({"approval_timeout_seconds": 60})
    assert _new_history(short)._retention == HISTORY_MIN_RETENTION

    long_cooldown = HealOpsConfig.model_validate(
        {"policies": {"restart_component": {"verdict": "allow", "cooldown_seconds": 3 * 86400}}}
    )
    assert _new_history(long_cooldown)._retention == timedelta(days=3) + HISTORY_MARGIN

    long_approval = HealOpsConfig.model_validate({"approval_timeout_seconds": 7 * 86400})
    assert _new_history(long_approval)._retention == timedelta(days=7) + HISTORY_MARGIN


# ----- warnings for things the config watches but the app never reports -----


class OnlyLatency:
    def get_health(self):
        return True

    def get_metrics(self):
        return {"latency_ms": 10.0}


class NoMetrics:
    def get_health(self):
        return True


WATCHING = {
    "thresholds": {
        "latency_ms": 500,
        "error_rate": 0.05,
        "drift_score": 0.3,
        "consecutive_breaches": 2,
    },
    "metric_degradation": {"f1_score": {"baseline": 0.9, "max_change": 0.1}},
}


def warnings_over(system, ticks):
    return [w for _ in range(ticks) for w in system.tick().warnings]


def test_a_metric_the_config_watches_but_the_app_never_reports_is_warned_about_once():
    system = make(OnlyLatency(), warn_unreported_after_checks=3, **WATCHING)
    assert warnings_over(system, 2) == []  # not enough checks yet
    found = system.tick().warnings
    text = "\n".join(found)
    assert "'error_rate'" in text and "'f1_score'" in text
    assert "any drift reading" in text
    assert "latency_ms" not in text
    assert "has not reported it in 3 checks" in text
    assert "does not provide drift readings" in text
    assert warnings_over(system, 5) == []  # said once, not every tick
    assert system.stats.warnings == len(found)


def test_an_app_without_a_provider_is_told_so():
    system = make(NoMetrics(), warn_unreported_after_checks=1, **WATCHING)
    text = "\n".join(system.tick().warnings)
    assert "does not provide metric readings at all" in text


def test_nothing_is_warned_when_everything_is_reported():
    class Everything:
        def get_health(self):
            return True

        def get_metrics(self):
            return {"latency_ms": 1.0, "error_rate": 0.0, "f1_score": 0.9}

        def get_drift_scores(self):
            return {"x": 0.0}

    system = make(Everything(), warn_unreported_after_checks=1, **WATCHING)
    assert warnings_over(system, 5) == []


def test_the_warning_can_be_switched_off():
    system = make(OnlyLatency(), warn_unreported_after_checks=0, **WATCHING)
    assert warnings_over(system, 10) == []


def test_a_healthy_toy_app_with_the_default_setup_gets_no_noise():
    system = make(ToyApp(), component=COMPONENT)
    assert warnings_over(system, 8) == []