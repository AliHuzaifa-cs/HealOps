"""Closing an escalated incident: by a person (`healops resolve`) or because it went away."""

from datetime import UTC, datetime

import pytest

from healops import HealOps
from healops.cli import main
from healops.config import HealOpsConfig
from healops.healing.notify import RecordingNotifier
from healops.store import ACKNOWLEDGED, ESCALATED, RECOVERED, StateStore
from healops.testing.harness import DEFAULT_CONFIG


class Flaky:
    """Unhealthy until `healthy` is set; its actions never help."""

    healthy = False

    def get_health(self):
        return self.healthy

    def restart_component(self, component):
        return "restarted"

    def reload_model(self):
        return "reloaded"


def make(tmp_path, adapter, **config):
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": str(tmp_path / "audit.jsonl"),
        "state_db_path": str(tmp_path / "state.db"),
        "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
        **config,
    }
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None)
    system.register("app", adapter, component="c")
    return system


def escalate(system, ticks=8):
    for _ in range(ticks):
        system.tick()


def status_of(tmp_path):
    store = StateStore(tmp_path / "state.db")
    try:
        return [(s.status, s.reason) for s in store.incidents()]
    finally:
        store.close()


def test_recovery_checks_default_and_bounds():
    assert HealOpsConfig().recovery_checks == 3
    for bad in (-1, 1001):
        with pytest.raises(ValueError):
            HealOpsConfig.model_validate({"recovery_checks": bad})


def test_an_escalated_incident_closes_itself_when_the_signal_stays_normal(tmp_path):
    app = Flaky()
    system = make(tmp_path, app, recovery_checks=3)
    escalate(system)
    assert [s for s, _ in status_of(tmp_path)] == [ESCALATED]

    app.healthy = True
    system.tick()
    system.tick()
    assert [s for s, _ in status_of(tmp_path)] == [ESCALATED]  # two normal checks are not enough
    system.tick()
    [(status, reason)] = status_of(tmp_path)
    assert status == RECOVERED
    assert "recovered on its own" in reason
    assert (tmp_path / "audit.jsonl").read_text().count('"stage":"resolved"') + (
        (tmp_path / "audit.jsonl").read_text().count('"stage": "resolved"')
    ) == 1


def test_one_normal_reading_in_the_middle_of_trouble_does_not_close_it(tmp_path):
    app = Flaky()
    system = make(tmp_path, app, recovery_checks=3)
    escalate(system)
    for healthy in (True, True, False, True, True):
        app.healthy = healthy
        system.tick()
    assert [s for s, _ in status_of(tmp_path)] == [ESCALATED]


def test_recovery_can_be_switched_off(tmp_path):
    app = Flaky()
    system = make(tmp_path, app, recovery_checks=0)
    escalate(system)
    app.healthy = True
    for _ in range(6):
        system.tick()
    assert [s for s, _ in status_of(tmp_path)] == [ESCALATED]


def test_the_same_problem_is_detected_again_after_it_recovered(tmp_path):
    app = Flaky()
    system = make(tmp_path, app, recovery_checks=2)
    escalate(system)
    app.healthy = True
    for _ in range(3):
        system.tick()
    app.healthy = False
    escalate(system)
    statuses = sorted(s for s, _ in status_of(tmp_path))
    assert statuses == [ESCALATED, RECOVERED]


def test_resolve_from_the_command_line_closes_an_escalated_incident(tmp_path):
    app = Flaky()
    system = make(tmp_path, app, recovery_checks=0)
    escalate(system)
    store = StateStore(tmp_path / "state.db")
    incident_id = store.incidents([ESCALATED])[0].incident.incident_id
    store.close()

    lines = []
    code = main(
        ["resolve", incident_id[:8], "--by", "ali", "--reason", "fixed by hand", "--no-wait",
         "--db", str(tmp_path / "state.db")],
        out=lines.append,
        now=lambda: datetime.now(UTC),
    )
    assert code == 0
    assert "recorded your resolution" in "\n".join(lines)

    system.tick()  # the running HealOps picks the answer up
    [(status, reason)] = status_of(tmp_path)
    assert status == ACKNOWLEDGED
    assert "ali" in reason and "fixed by hand" in reason


def test_resolve_refuses_an_incident_that_is_not_escalated(tmp_path):
    make(tmp_path, Flaky())  # creates the state file; nothing escalated
    lines = []
    code = main(
        ["resolve", "abcd1234", "--by", "ali", "--no-wait", "--db", str(tmp_path / "state.db")],
        out=lines.append,
    )
    assert code == 2
    assert "no escalated incident" in "\n".join(lines)
