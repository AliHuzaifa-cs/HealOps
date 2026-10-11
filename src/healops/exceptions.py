"""Exceptions raised by HealOps."""


class HealOpsError(Exception):
    """Base class for all HealOps errors."""


class InvalidTransitionError(HealOpsError):
    """Raised when an incident is asked to move to a state it cannot reach."""