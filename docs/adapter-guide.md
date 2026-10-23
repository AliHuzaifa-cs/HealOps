# Connect your application: the adapter guide

HealOps never touches your application directly. You give it an **adapter**: a plain Python
object whose methods HealOps may call. Implement only what your application really has;
HealOps checks which methods exist and offers the agent only the actions that are possible.

## The methods

| Method | Returns | What HealOps does with it | Needed for |
| --- | --- | --- | --- |
| `get_health()` | `bool` | `False` (or an error or a timeout) means unhealthy | detecting crashes |
| `get_metrics()` | `dict[str, float]` | compares named numbers with your thresholds and baselines | slow service, errors, model quality |
| `get_drift_scores()` | `dict[str, float]` | compares drift scores with `thresholds.drift_score` | data drift |
| `restart_component(component)` | `str` | restarts one named component | action `restart_component` |
| `reload_model()` | `str` | reloads the current model artifact | action `reload_model` |
| `rollback_model()` | `str` | returns to a known-good model version | action `rollback_model` |
| `request_retraining()` | `str` | starts a retraining run (does **not** deploy the result) | action `request_retraining` |
| `request_redeployment()` | `str` | redeploys (decides what is live) | action `request_redeployment` |

Action methods return a short message describing what happened (it goes in the audit log).
If something fails, **raise an exception**: HealOps treats it as a failed action, does not
retry it by itself and asks a person.

## Rules your adapter must follow

1. **Return real numbers.** Metrics and drift scores must be finite numbers (`bool`, `NaN`
   and infinity are ignored). Never invent a value when you cannot read one: raise instead.
2. **Use the metric names the config watches.** `latency_ms` and `error_rate` are matched by
   the `thresholds` section; model-quality metrics are matched by the names you list under
   `metric_degradation` (for example `f1_score`). If a watched name is never reported,
   HealOps warns once after `warn_unreported_after_checks` checks.
3. **Be safe to call from another thread.** Every call runs in a helper thread with a time
   limit (`adapter_timeouts`). Do not share objects that must stay on one thread (for
   example a SQLite connection created in the main thread).
4. **Return quickly or raise.** A read that takes longer than `read_seconds` counts as a
   failed reading; an action that takes longer than `action_seconds` is escalated to a
   person and **not** retried, because HealOps cannot know whether it finished.
5. **Make actions safe to repeat.** A restart that happens twice should do no harm.
6. **Keep secrets out of messages.** What you return and raise is written to logs (HealOps
   hides well-known secret shapes, but that is a safety net, not a guarantee).

## Example

A complete, runnable adapter for an in-memory service (swap the bodies for calls to your
real service):

```python
# run-in-tests
from healops import HealOps


class MyServiceAdapter:
    """Pretends to be a model API. Replace the bodies with real calls."""

    def __init__(self):
        self.up = False
        self.restarts = 0

    def get_health(self) -> bool:
        return self.up

    def get_metrics(self) -> dict[str, float]:
        return {"latency_ms": 120.0, "error_rate": 0.0}

    def restart_component(self, component: str) -> str:
        self.restarts += 1
        self.up = True
        return f"restarted {component}"


config = {
    "dry_run": False,
    "audit_log_path": None,   # keep the audit log in memory for this example
    "state_db_path": None,    # and the state too
    "min_confidence_for_auto": 0.3,
    "thresholds": {"latency_ms": 500, "consecutive_breaches": 2},
    "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
    "policies": {"restart_component": {"verdict": "allow", "cooldown_seconds": 0}},
}

adapter = MyServiceAdapter()
healops = HealOps(config)
healops.register("my-service", adapter, component="api-server")
for _ in range(5):
    healops.tick()
assert adapter.up and adapter.restarts == 1
print("recovered after", adapter.restarts, "restart")
```

`register(name, adapter, component=...)` needs `component` whenever the adapter can restart
components: it is the name passed to `restart_component`. Several applications can be
registered under different names; each keeps its own component.

## Running it

```python
healops.run()          # monitor until healops.stop() is called (for example from a signal handler)
healops.run(max_ticks=10)   # or a fixed number of checks (handy in tests)
healops.tick()         # a single check
```

Useful calls while it runs: `healops.pending_approvals()`, `healops.incidents()`,
`healops.approve(...)`, `healops.reject(...)`, `healops.acknowledge(incident_id)` and
`healops.stuck_calls()`. People can also answer from another terminal with the `healops`
command (see the README).

## Start in dry-run

`dry_run` is **on by default**: proposed actions are logged and alerted, never executed.
Run like that first, read the audit log (`healops_audit.jsonl`), and only then set
`dry_run: false` and give the actions you trust the verdict `allow`.

## Turning off the time limits

`healops.register(name, adapter, component=..., guard=False)` calls the adapter directly in
the monitoring thread. Only do this if the adapter cannot be called from another thread, and
accept that a hanging call then hangs the loop.