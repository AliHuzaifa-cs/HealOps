from healops.config import ThresholdsConfig
from healops.detection.thresholds import build_threshold_detector
from healops.events import Observation
from healops.types import IncidentType, ObservationKind


def drift(app, name, value):
    return Observation(app_name=app, kind=ObservationKind.DRIFT, name=name, value=value)


def cfg(n=2):
    return ThresholdsConfig(drift_score=0.25, consecutive_breaches=n)


def tick(det, **values):
    return det.evaluate([drift("psx", k, v) for k, v in values.items()])


def test_two_drift_signals_make_one_incident():
    det = build_threshold_detector(cfg())
    assert tick(det, prob_up_psi=1.0, prob_up_ks=0.9) == []
    out = tick(det, prob_up_psi=1.0, prob_up_ks=0.9)
    assert len(out) == 1 and out[0].incident_type is IncidentType.DATA_DRIFT
    assert {o.name for o in out[0].evidence} == {"prob_up_psi", "prob_up_ks"}


def test_a_later_signal_does_not_open_a_second_incident():
    det = build_threshold_detector(cfg())
    tick(det, a=1.0, b=0.0); first = tick(det, a=1.0, b=0.0)
    assert len(first) == 1
    tick(det, a=1.0, b=1.0); later = tick(det, a=1.0, b=1.0)
    assert later == []


def test_release_allows_the_problem_to_come_back():
    det = build_threshold_detector(cfg())
    tick(det, a=1.0); inc = tick(det, a=1.0)[0]
    det.release(inc.incident_id)
    assert tick(det, a=1.0) == []
    assert len(tick(det, a=1.0)) == 1


def test_other_app_is_separate():
    det = build_threshold_detector(cfg())
    det.evaluate([drift("a", "x", 1.0), drift("b", "x", 1.0)])
    out = det.evaluate([drift("a", "x", 1.0), drift("b", "x", 1.0)])
    assert {i.app_name for i in out} == {"a", "b"}


def test_hold_after_restart_blocks_duplicates():
    det = build_threshold_detector(cfg())
    tick(det, a=1.0, b=1.0); inc = tick(det, a=1.0, b=1.0)[0]
    fresh = build_threshold_detector(cfg())
    fresh.hold(inc)
    tick(fresh, a=1.0, b=1.0)
    assert tick(fresh, a=1.0, b=1.0) == []


def test_single_signal_behaves_as_before():
    det = build_threshold_detector(cfg(3))
    assert tick(det, a=1.0) == [] and tick(det, a=1.0) == []
    assert len(tick(det, a=1.0)) == 1 and tick(det, a=1.0) == []
