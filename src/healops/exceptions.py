"""Exceptions raised by HealOps."""


class HealOpsError(Exception):
    """Base class for all HealOps errors."""


class InvalidTransitionError(HealOpsError):
    """Raised when an incident is asked to move to a state it cannot reach."""


class ApplicationAlreadyRegisteredError(HealOpsError):
    """Raised when an application name is registered twice."""


class UnknownApplicationError(HealOpsError):
    """Raised when an application name has not been registered."""