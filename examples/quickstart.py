"""HealOps in a few lines: a toy app crashes and HealOps heals it.

Run:  python examples/quickstart.py
"""

from healops import HealOps
from healops.testing.toy_app import COMPONENT, Fault, ToyApp

config = {
    "dry_run": False,  # let HealOps really act (the default is a safe dry-run)
    "audit_log_path": "quickstart_audit.jsonl",
    "check_interval_seconds": 1,
    "min_confidence_for_auto": 0.3,
    "thresholds": {"latency_ms": 500, "error_rate": 0.05, "consecutive_breaches": 2},
    "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
    "policies": {"restart_component": {"verdict": "allow", "cooldown_seconds": 0}},
}

app = ToyApp()
healops = HealOps(config)
healops.register("toy-app", app, component=COMPONENT)

app.inject(Fault.CRASH)
print("healthy before:", app.get_health())
healops.run(max_ticks=6)
print("healthy after: ", app.get_health())
print("actions taken: ", app.actions)