from datetime import UTC, datetime, timedelta

import pytest

from healops.healing.notify import AlertKind
from healops.healing.orchestrator import HandleStatus
from healops.loop import MonitoringLoop, TickReport
from healops.registry import ApplicationRegistry
from healops.testing import Fault, make_toy_system
from healops.types import IncidentType

T0 = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_loop(system=None, **kwargs):
    system = system or make_toy_system()
    loop = MonitoringLoop(
        system.registry, system.detector, system.orchestrator, wait=lambda s: False, **kwargs
    )
    return system, loop


def approval_config():
    return {
        "min_confidence_for_auto": 0.3,
        "thresholds": {"error_rate": 0.05, "consecutive_breaches": 2},
        "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
        "policies": {"rollback_model": {"verdict": "needs_approval", "cooldown_seconds": 0}},
    }


# ---------- a normal tick ----------


def test_a_tick_on_a_healthy_app_reads_it_and_finds_nothing():
    _, loop = make_loop()
    report = loop.tick()
    assert report.tick == 1
    assert report.observations == 5  # health, 3 metrics, 1 drift score
    assert report.detected == ()
    assert report.errors == ()
    assert loop.stats.ticks == 1


def test_with_no_applications_a_tick_does_nothing():
    system = make_toy_system()
    loop = MonitoringLoop(
        ApplicationRegistry(), system.detector, system.orchestrator, wait=lambda s: False
    )
    assert loop.tick() == TickReport(tick=1)


def test_a_fault_is_detected_on_the_second_tick_and_healed():
    system, loop = make_loop()
    system.app.inject(Fault.CRASH)
    first = loop.tick()
    assert first.detected == ()  # one breach is not enough
    second = loop.tick()
    assert [i.incident_type for i in second.detected] == [IncidentType.HEALTH_FAILURE]
    assert second.results[0].status is HandleStatus.RESOLVED
    assert system.app.faults == set()
    assert loop.stats.resolved == 1
    assert loop.open_incidents == {}


def test_after_healing_the_same_fault_can_be_detected_again():
    system, loop = make_loop()
    system.app.inject(Fault.CRASH)
    loop.tick()
    loop.tick()
    system.app.inject(Fault.CRASH)
    loop.tick()
    again = loop.tick()
    assert len(again.detected) == 1
    assert loop.stats.resolved == 2


# ---------- incidents that need a person ----------


def test_an_escalated_incident_alerts_once_and_is_not_raised_again_every_tick():
    system, loop = make_loop()
    system.app.inject(Fault.DRIFT)
    for _ in range(6):
        loop.tick()
    assert [a.kind for a in system.alerts.alerts] == [AlertKind.ESCALATED]
    assert loop.stats.incidents == 1
    assert len(loop.open_incidents) == 1


def test_acknowledging_an_escalated_incident_lets_it_be_detected_again():
    system, loop = make_loop()
    system.app.inject(Fault.DRIFT)
    loop.tick()
    loop.tick()
    (incident_id,) = loop.open_incidents
    loop.acknowledge(incident_id)
    assert loop.open_incidents == {}
    loop.tick()
    again = loop.tick()
    assert len(again.detected) == 1  # the drift is still there, so a new incident starts


def test_an_action_that_needs_approval_waits_and_then_runs_when_approved():
    system, loop = make_loop(make_toy_system(approval_config()))
    system.app.inject(Fault.BAD_MODEL)
    loop.tick()
    report = loop.tick()
    assert report.results[0].status is HandleStatus.PARKED
    assert system.app.actions == []
    assert loop.stats.parked == 1

    parked = report.results[0]
    done = loop.approve(parked.incident.incident_id, parked.proposal.proposal_id, "ali")
    assert done.status is HandleStatus.RESOLVED
    assert system.app.actions == ["rollback_model"]
    assert loop.open_incidents == {}


def test_a_rejected_action_never_runs_and_stays_open_for_a_person():
    system, loop = make_loop(make_toy_system(approval_config()))
    system.app.inject(Fault.BAD_MODEL)
    loop.tick()
    parked = loop.tick().results[0]
    result = loop.reject(parked.incident.incident_id, parked.proposal.proposal_id, "ali", "no")
    assert result.status is HandleStatus.ESCALATED
    assert system.app.actions == []
    assert parked.incident.incident_id in loop.open_incidents


def test_an_approval_that_waits_too_long_is_escalated_by_a_later_tick():
    clock = Clock()
    config = {**approval_config(), "approval_timeout_seconds": 60}
    system, loop = make_loop(make_toy_system(config, clock=clock))
    system.app.inject(Fault.BAD_MODEL)
    loop.tick()
    loop.tick()
    assert system.alerts.alerts[-1].kind is AlertKind.APPROVAL_NEEDED
    clock.advance(61)
    report = loop.tick()
    assert report.expired[0].status is HandleStatus.ESCALATED
    assert system.app.actions == []  # silence never approves anything
    assert system.alerts.alerts[-1].kind is AlertKind.ESCALATED


# ---------- failures never stop the loop ----------


def test_a_failing_application_does_not_stop_the_others():
    system = make_toy_system()

    class Broken:
        def get_health(self):
            raise ConnectionError("down password=hunter2")

    system.registry.register("broken-app", Broken())
    _, loop = make_loop(system)
    system.app.inject(Fault.CRASH)
    loop.tick()
    report = loop.tick()
    assert any(r.status is HandleStatus.RESOLVED for r in report.results)
    assert system.app.faults == set()
    assert "hunter2" not in " ".join(report.errors)


def test_readings_that_cannot_be_read_are_reported_per_application():
    system = make_toy_system()

    class Odd:
        def get_health(self):
            return True

        def get_metrics(self):
            raise TimeoutError("slow")

    system.registry.register("odd-app", Odd())
    _, loop = make_loop(system)
    report = loop.tick()
    assert report.errors == ("odd-app: get_metrics failed (TimeoutError)",)


def test_an_unexpected_error_while_handling_is_reported_and_the_incident_is_retried():
    system, loop = make_loop()
    original = system.orchestrator.handle
    calls = []

    def flaky(incident, recent=()):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug password=hunter2")
        return original(incident, recent)

    system.orchestrator.handle = flaky  # type: ignore[method-assign]
    system.app.inject(Fault.CRASH)
    loop.tick()
    failed = loop.tick()
    assert failed.errors == ("handling an incident on toy-app failed (RuntimeError)",)
    assert failed.results == ()
    assert system.app.faults == {Fault.CRASH}
    loop.tick()
    retried = loop.tick()  # released, so detected again after two more breaches
    assert retried.results[0].status is HandleStatus.RESOLVED


def test_a_broken_on_tick_callback_does_not_stop_the_loop():
    def broken(report):
        raise ValueError("oops")

    _, loop = make_loop(on_tick=broken)
    stats = loop.run(max_ticks=3)
    assert stats.ticks == 3


def test_a_broken_detector_is_reported_and_the_loop_keeps_going():
    system, loop = make_loop()

    def explode(observations):
        raise RuntimeError("bug")

    system.detector.evaluate = explode  # type: ignore[method-assign]
    report = loop.tick()
    assert report.errors == ("detecting for toy-app failed (RuntimeError)",)
    assert loop.tick().tick == 2


# ---------- running and stopping ----------


def test_run_stops_after_max_ticks_and_waits_between_ticks():
    waits = []
    system = make_toy_system()
    loop = MonitoringLoop(
        system.registry,
        system.detector,
        system.orchestrator,
        interval_seconds=30,
        wait=lambda s: waits.append(s) or False,
        monotonic=lambda: 0.0,
    )
    stats = loop.run(max_ticks=3)
    assert stats.ticks == 3
    assert waits == [30, 30]  # no wait after the last tick


def test_the_pause_is_shortened_by_the_time_a_tick_took():
    times = iter([0.0, 12.0, 100.0, 100.0, 100.0, 100.0])
    waits = []
    system = make_toy_system()
    loop = MonitoringLoop(
        system.registry,
        system.detector,
        system.orchestrator,
        interval_seconds=30,
        wait=lambda s: waits.append(s) or False,
        monotonic=lambda: next(times),
    )
    loop.run(max_ticks=2)
    assert waits == [18.0]


def test_a_tick_that_took_longer_than_the_interval_does_not_wait():
    times = iter([0.0, 50.0, 50.0, 50.0])
    waits = []
    system = make_toy_system()
    loop = MonitoringLoop(
        system.registry,
        system.detector,
        system.orchestrator,
        interval_seconds=30,
        wait=lambda s: waits.append(s) or False,
        monotonic=lambda: next(times),
    )
    loop.run(max_ticks=2)
    assert waits == [0.0]


def test_stop_ends_the_loop_after_the_current_tick():
    system = make_toy_system()
    holder = {}

    def stop_after_two(report):
        if report.tick == 2:
            holder["loop"].stop()

    loop = MonitoringLoop(
        system.registry,
        system.detector,
        system.orchestrator,
        wait=lambda s: False,
        on_tick=stop_after_two,
    )
    holder["loop"] = loop
    stats = loop.run()
    assert stats.ticks == 2
    assert loop.stopped


def test_a_wait_that_says_stop_ends_the_loop():
    system = make_toy_system()
    loop = MonitoringLoop(
        system.registry, system.detector, system.orchestrator, wait=lambda s: True
    )
    assert loop.run().ticks == 1


def test_the_default_wait_wakes_up_immediately_when_stopped():
    import threading

    system = make_toy_system()
    loop = MonitoringLoop(
        system.registry, system.detector, system.orchestrator, interval_seconds=3600
    )
    thread = threading.Thread(target=loop.run)
    thread.start()
    loop.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_bad_settings_are_refused():
    system = make_toy_system()
    with pytest.raises(ValueError):
        MonitoringLoop(system.registry, system.detector, system.orchestrator, interval_seconds=0)
    _, loop = make_loop(system)
    with pytest.raises(ValueError):
        loop.run(max_ticks=0)


def test_the_stats_add_up():
    system, loop = make_loop()
    system.app.inject(Fault.CRASH)
    system.app.inject(Fault.DRIFT)
    loop.run(max_ticks=4)
    stats = loop.stats
    assert stats.ticks == 4
    assert stats.incidents == 2
    assert stats.resolved == 1
    assert stats.escalated == 1
    assert stats.errors == 0