"""Building blocks shared by all detectors."""

from collections.abc import Sequence
from typing import Protocol

from healops.events import Incident, Observation


class Detector(Protocol):
    """Turns a batch of observations into incidents (possibly none)."""

    def evaluate(self, observations: Sequence[Observation]) -> list[Incident]: ...


class BreachTracker:
    """Counts breaches in a row so one odd reading does not raise an incident.

    `record` returns True exactly once: on the check where the number of
    breaches in a row first reaches `required`. While the problem continues it
    returns False (the incident already exists). A check that is not a breach
    resets the count. Call `reset` when an incident is closed so a problem that
    comes back can be detected again.
    """

    def __init__(self, required: int) -> None:
        if required < 1:
            raise ValueError("required must be at least 1")
        self._required = required
        self._counts: dict[str, int] = {}

    def record(self, key: str, breached: bool) -> bool:
        if not breached:
            self._counts[key] = 0
            return False
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count == self._required

    def reset(self, key: str) -> None:
        self._counts.pop(key, None)

    def count(self, key: str) -> int:
        return self._counts.get(key, 0)