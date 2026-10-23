"""Time limits for calls into an application's adapter.

An adapter belongs to the application, so HealOps cannot trust it to return. Every call
runs in a helper thread with a time limit. A call that does not finish raises
`AdapterTimeout`; the monitoring loop and the other applications carry on.

A stuck call cannot be killed (Python cannot stop a thread), so it is counted. After
`max_stuck_calls` stuck calls for one application, no new call is made until one of them
finishes: the application is treated as unresponsive instead of piling up threads.

Consequences to know about:
- Adapter methods must be safe to call from another thread than the one that created the
  adapter (for example, do not share a SQLite connection made in the main thread).
- A call that timed out may still be running. For an action, its outcome is unknown, so
  HealOps escalates to a person and never retries it by itself.
"""

import threading
from collections.abc import Callable
from typing import Any

from healops.exceptions import AdapterTimeout, AdapterUnresponsive

READ_METHODS = frozenset({"get_health", "get_metrics", "get_drift_scores"})
ACTION_METHODS = frozenset(
    {
        "restart_component",
        "reload_model",
        "rollback_model",
        "request_retraining",
        "request_redeployment",
    }
)
ALL_METHODS = READ_METHODS | ACTION_METHODS


class GuardedAdapter:
    """Wraps an adapter so each of its capability methods has a time limit."""

    def __init__(
        self,
        adapter: object,
        app_name: str,
        read_seconds: float,
        action_seconds: float,
        max_stuck_calls: int,
    ) -> None:
        if read_seconds <= 0 or action_seconds <= 0:
            raise ValueError("time limits must be positive")
        if max_stuck_calls < 1:
            raise ValueError("max_stuck_calls must be at least 1")
        self._adapter = adapter
        self._app_name = app_name
        self._read_seconds = read_seconds
        self._action_seconds = action_seconds
        self._max_stuck = max_stuck_calls
        self._lock = threading.Lock()
        self._stuck: set[threading.Thread] = set()
        # Set on the instance (not via __getattr__) so capability checks can see them.
        for name in sorted(ALL_METHODS):
            target = getattr(adapter, name, None)
            if callable(target):
                setattr(self, name, self._wrap(name, target))

    @property
    def wrapped(self) -> object:
        """The real adapter."""
        return self._adapter

    @property
    def stuck_calls(self) -> int:
        """How many timed-out calls are still running."""
        with self._lock:
            self._stuck = {t for t in self._stuck if t.is_alive()}
            return len(self._stuck)

    def _wrap(self, name: str, target: Callable[..., Any]) -> Callable[..., Any]:
        limit = self._read_seconds if name in READ_METHODS else self._action_seconds

        def guarded(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, limit, target, args, kwargs)

        guarded.__name__ = name
        return guarded

    def _call(
        self,
        name: str,
        limit: float,
        target: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        if self.stuck_calls >= self._max_stuck:
            raise AdapterUnresponsive(
                f"{name} on {self._app_name} was not attempted: {self._max_stuck} earlier "
                "calls are still stuck"
            )
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["value"] = target(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001 - handed back to the caller's thread
                box["error"] = error

        thread = threading.Thread(target=work, name=f"healops-{self._app_name}-{name}", daemon=True)
        thread.start()
        thread.join(limit)
        if thread.is_alive():
            with self._lock:
                self._stuck.add(thread)
            suffix = (
                "; it may still be running, so its outcome is unknown"
                if name in ACTION_METHODS
                else ""
            )
            raise AdapterTimeout(f"{name} on {self._app_name} timed out after {limit:g}s{suffix}")
        if "error" in box:
            raise box["error"]
        return box["value"]