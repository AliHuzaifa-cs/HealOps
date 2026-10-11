from collections.abc import Sequence

import pytest

from healops.detection.base import BreachTracker, Detector
from healops.events import Incident, Observation


def test_fires_only_after_the_required_breaches_in_a_row():
    tracker = BreachTracker(required=3)
    assert tracker.record("app:latency", True) is False
    assert tracker.record("app:latency", True) is False
    assert tracker.record("app:latency", True) is True


def test_fires_once_while_the_problem_continues():
    tracker = BreachTracker(required=2)
    results = [tracker.record("k", True) for _ in range(5)]
    assert results == [False, True, False, False, False]


def test_one_good_reading_resets_the_count():
    tracker = BreachTracker(required=3)
    tracker.record("k", True)
    tracker.record("k", True)
    tracker.record("k", False)
    assert tracker.count("k") == 0
    assert tracker.record("k", True) is False


def test_a_problem_that_clears_and_returns_fires_again():
    tracker = BreachTracker(required=2)
    tracker.record("k", True)
    assert tracker.record("k", True) is True
    tracker.record("k", False)
    tracker.record("k", True)
    assert tracker.record("k", True) is True


def test_reset_allows_redetection_while_still_breaching():
    tracker = BreachTracker(required=2)
    tracker.record("k", True)
    assert tracker.record("k", True) is True
    tracker.reset("k")
    tracker.record("k", True)
    assert tracker.record("k", True) is True


def test_keys_are_counted_separately():
    tracker = BreachTracker(required=2)
    tracker.record("a", True)
    tracker.record("b", True)
    assert tracker.record("a", True) is True
    assert tracker.count("b") == 1


def test_required_of_one_fires_immediately():
    assert BreachTracker(required=1).record("k", True) is True


@pytest.mark.parametrize("bad", [0, -3])
def test_required_must_be_at_least_one(bad):
    with pytest.raises(ValueError):
        BreachTracker(required=bad)


def test_unseen_key_has_a_zero_count():
    assert BreachTracker(required=2).count("never-seen") == 0


def test_any_object_with_evaluate_is_a_detector():
    class NoOpDetector:
        def evaluate(self, observations: Sequence[Observation]) -> list[Incident]:
            return []

    detector: Detector = NoOpDetector()
    assert detector.evaluate([]) == []