"""The HealOps dashboard: a small web page that shows what HealOps saw and did.

It reads the same state file and audit log that the `healops` command uses, so it needs no
extra service and no extra install. Start it with `healops dashboard`. To look around with
labelled sample data first, use `healops demo`.
"""

from healops.dashboard.server import DashboardApp, serve

__all__ = ["DashboardApp", "serve"]
