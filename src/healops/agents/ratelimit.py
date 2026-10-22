"""A call budget for a diagnosis agent, so a busy or flapping system cannot run up a bill.

When the budget for the period is used up, the agent is not called at all and
RateLimitExceeded is raised. Put this inside a FallbackAgent so the rule-based
agent answers instead (its answers are held back for a person's approval).
"""

import threading
import time
from collections import deque
from collections.abc import Callable

from healops.agents.base import AgentResult, DiagnosisAgent, DiagnosisContext
from healops.exceptions import RateLimitExceeded


class RateLimitedAgent:
    """Allows at most `max_calls` calls to `agent` in any `per_seconds` window.

    Every attempt counts, including ones that fail, because a failed call to a
    paid service usually costs too.
    """

    def __init__(
        self,
        agent: DiagnosisAgent,
        max_calls: int,
        per_seconds: float = 3600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if per_seconds <= 0:
            raise ValueError("per_seconds must be positive")
        self._agent = agent
        self._max = max_calls
        self._window = per_seconds
        self._monotonic = monotonic
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.refused_count = 0

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()

    @property
    def calls_used(self) -> int:
        with self._lock:
            self._trim(self._monotonic())
            return len(self._calls)

    @property
    def calls_left(self) -> int:
        return self._max - self.calls_used

    def diagnose(self, context: DiagnosisContext) -> AgentResult:
        with self._lock:
            now = self._monotonic()
            self._trim(now)
            if len(self._calls) >= self._max:
                self.refused_count += 1
                raise RateLimitExceeded(
                    f"call budget used up ({self._max} per {self._window:g} seconds)"
                )
            self._calls.append(now)
        return self._agent.diagnose(context)