"""The `healops` command: answering approvals from another terminal."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from healops import HealOps
from healops.cli import EXIT_FAILED, EXIT_NOT_YET, EXIT_OK, EXIT_USAGE, main
from healops.healing.notify import RecordingNotifier
from healops.store import APPLIED, ESCALATED, FAILED, RESOLVED, WAITING, StateStore
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, Fault, ToyApp

NEEDS_APPROVAL = {"restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0}}
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Console:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def world(tmp_path):
    """A running HealOps with one crashed app waiting for approval, and a way to call the CLI."""
    clock = Clock()
    path = tmp_path / "state.db"
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": str(path),
        "approval_timeout_seconds": 600,
        "policies": {**DEFAULT_CONFIG["policies"], **NEEDS_APPROVAL},
    }
    system = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None, clock=clock)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    for _ in range(4):
        system.tick()

    class World:
        pass

    w = World()
    w.system, w.app, w.path, w.clock = system, app, path, clock
    w.incident_id = system.pending_approvals()[0].incident.incident_id
    w.out = Console()

    def cli(*argv, sleep=lambda _s: None):
        w.out = Console()
        code = main([*argv, "--db", str(path)], out=w.out, now=clock, sleep=sleep)
        return code

    w.cli = cli
    return w


def test_list_shows_the_waiting_approval_and_the_heartbeat(world):
    assert world.cli("list") == EXIT_OK
    text = world.out.text
    assert world.incident_id in text
    assert "restart_component" in text and f"component={COMPONENT}" in text
    assert "Waiting for approval (1)" in text
    assert "HealOps last active: 0s ago" in text
    assert "WARNING" not in text


def test_list_warns_when_healops_looks_stopped(world):
    world.clock.now = T0 + timedelta(minutes=10)
    world.cli("list")
    assert "WARNING" in world.out.text


def test_list_on_an_empty_state_says_nothing_is_waiting(tmp_path):
    StateStore(tmp_path / "s.db").close()
    out = Console()
    assert main(["list", "--db", str(tmp_path / "s.db")], out=out, now=lambda: T0) == EXIT_OK
    assert out.text.count("  nothing\n") + out.text.endswith("  nothing") == 2
    assert "never" in out.text


def test_approving_records_a_decision_and_changes_nothing_yet(world):
    code = world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    assert code == EXIT_OK
    [decision] = world.system.store.waiting_decisions()
    assert (decision.action, decision.approver, decision.status) == ("approve", "ali", WAITING)
    assert world.app.actions == []


def test_the_running_healops_applies_the_decision_on_its_next_check(world):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    world.system.tick()
    assert world.app.actions == [f"restart_component:{COMPONENT}"]
    assert world.app.get_health() is True
    assert world.system.store.waiting_decisions() == []
    assert world.system.store.incidents([RESOLVED])
    assert world.system.stats.decisions == 1


def test_by_default_the_command_waits_and_reports_the_result(world):
    code = world.cli("approve", world.incident_id, "--by", "ali", sleep=lambda _s: world.system.tick())
    assert code == EXIT_OK
    assert "done: the incident is now resolved" in world.out.text


def test_rejecting_escalates_and_keeps_the_reason(world):
    code = world.cli(
        "reject", world.incident_id, "--by", "ali", "--reason", "not now",
        sleep=lambda _s: world.system.tick(),
    )
    assert code == EXIT_OK
    assert world.app.actions == []
    [stored] = world.system.store.incidents([ESCALATED])
    assert "rejected by ali" in (stored.reason or "") and "not now" in (stored.reason or "")


def test_a_decision_is_applied_only_once(world):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    for _ in range(3):
        world.system.tick()
    assert world.app.actions == [f"restart_component:{COMPONENT}"]
    assert world.system.stats.decisions == 1


def test_the_start_of_the_incident_id_is_enough(world):
    assert world.cli("approve", world.incident_id[:8], "--by", "ali", "--no-wait") == EXIT_OK


def test_too_short_unknown_and_ambiguous_ids_are_refused(world):
    assert world.cli("approve", world.incident_id[:2], "--by", "ali") == EXIT_USAGE
    assert "at least 4" in world.out.text
    assert world.cli("approve", "ffffffffffff", "--by", "ali") == EXIT_USAGE
    assert "nothing is waiting" in world.out.text
    assert world.system.store.waiting_decisions() == []


def test_two_answers_for_the_same_incident_are_refused(world):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    assert world.cli("reject", world.incident_id, "--by", "sara", "--no-wait") == EXIT_USAGE
    assert "already waiting" in world.out.text
    assert len(world.system.store.waiting_decisions()) == 1


@pytest.mark.parametrize("name", ["", "   ", "al\x00i", "a\nb", "x" * 101])
def test_a_bad_approver_name_is_refused(world, name):
    assert world.cli("approve", world.incident_id, "--by", name, "--no-wait") == EXIT_USAGE
    assert world.system.store.waiting_decisions() == []


def test_a_too_long_reason_is_refused(world):
    code = world.cli("reject", world.incident_id, "--by", "ali", "--reason", "r" * 501, "--no-wait")
    assert code == EXIT_USAGE


def test_if_healops_does_not_answer_in_time_the_command_says_so(world):
    code = world.cli("approve", world.incident_id, "--by", "ali", "--timeout", "0.05")
    assert code == EXIT_NOT_YET
    assert "saved and will be applied" in world.out.text
    assert world.app.actions == []


def test_a_late_answer_is_never_applied(world):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    world.clock.now = T0 + timedelta(hours=2)  # the approval window (600s) is over
    world.system.tick()
    assert world.app.actions == []
    [decision] = [d for d in _all_decisions(world)]
    assert decision.status == FAILED
    assert "Expired" in (decision.outcome or "")
    assert world.system.store.incidents([ESCALATED])


def _all_decisions(world):
    return world.system.store._decisions("ORDER BY id", ())


def test_the_command_reports_a_failed_decision(world):
    world.clock.now = T0 + timedelta(hours=2)
    code = world.cli(
        "approve", world.incident_id, "--by", "ali", "--timeout", "5",
        sleep=lambda _s: world.system.tick(),
    )
    assert code == EXIT_FAILED
    assert "not applied" in world.out.text


def test_an_answer_for_a_different_proposal_is_not_applied(world):
    world.system.store.add_decision(world.incident_id, "not-the-proposal", "approve", "ali")
    world.system.tick()
    assert world.app.actions == []
    [decision] = _all_decisions(world)
    assert decision.status == FAILED
    assert len(world.system.pending_approvals()) == 1


def test_an_answer_about_an_unknown_incident_fails_without_stopping_the_loop(world):
    world.system.store.add_decision("nope", "nope", "approve", "ali")
    report = world.system.tick()
    assert report.errors == ()
    assert _all_decisions(world)[0].status == FAILED


def test_an_answer_waits_for_the_next_start_if_healops_is_down(world, tmp_path):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    world.system.close()
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": str(world.path),
        "approval_timeout_seconds": 600,
        "policies": {**DEFAULT_CONFIG["policies"], **NEEDS_APPROVAL},
    }
    second = HealOps(data, notifiers=[RecordingNotifier()], sleep=lambda _s: None, clock=world.clock)
    app = ToyApp()
    second.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    second.tick()
    assert app.actions == [f"restart_component:{COMPONENT}"]
    assert second.store.waiting_decisions() == []


def test_a_missing_state_file_is_an_error_and_is_not_created(tmp_path):
    out = Console()
    missing = tmp_path / "nope.db"
    assert main(["list", "--db", str(missing)], out=out) == EXIT_USAGE
    assert "no state file" in out.text
    assert not missing.exists()


def test_the_state_file_can_come_from_a_config(world, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text(f"state_db_path: '{world.path.as_posix()}'\n")
    out = Console()
    assert main(["list", "--config", str(config)], out=out, now=world.clock) == EXIT_OK
    assert world.incident_id in out.text


def test_a_config_that_keeps_state_in_memory_is_explained(tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("state_db_path: null\n")
    out = Console()
    assert main(["list", "--config", str(config)], out=out) == EXIT_USAGE
    assert "memory" in out.text


def test_it_runs_as_a_module(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "healops", "list", "--db", str(tmp_path / "none.db")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == EXIT_USAGE
    assert "no state file" in result.stdout


def test_the_heartbeat_is_written_every_tick(world):
    before = world.system.store.last_beat()
    world.clock.now = T0 + timedelta(seconds=30)
    world.system.tick()
    assert world.system.store.last_beat() == T0 + timedelta(seconds=30)
    assert before == T0


def test_applied_status_name_matches_what_the_command_prints(world):
    world.cli("approve", world.incident_id, "--by", "ali", "--no-wait")
    world.system.tick()
    assert _all_decisions(world)[0].status == APPLIED