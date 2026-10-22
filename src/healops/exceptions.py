"""Exceptions raised by HealOps."""


class HealOpsError(Exception):
    """Base class for all HealOps errors."""


class InvalidTransitionError(HealOpsError):
    """Raised when an incident is asked to move to a state it cannot reach."""


class ApplicationAlreadyRegisteredError(HealOpsError):
    """Raised when an application name is registered twice."""


class UnknownApplicationError(HealOpsError):
    """Raised when an application name has not been registered."""


class ConfigError(HealOpsError):
    """Raised when a configuration file is missing, unreadable or invalid."""


class RecordError(HealOpsError):
    """Raised when a record cannot be converted to or from JSON."""


class ApprovalError(HealOpsError):
    """Base class for problems with a human approval."""


class NoPendingApprovalError(ApprovalError):
    """Raised when nothing is waiting for approval for the given incident and proposal."""


class ApprovalExpiredError(ApprovalError):
    """Raised when someone tries to approve after the approval window has closed."""


class AuditError(HealOpsError):
    """Base class for audit log problems."""


class AuditWriteError(AuditError):
    """Raised when an audit event could not be written."""


class AuditReadError(AuditError):
    """Raised when an audit log file cannot be read back."""

class LLMError(HealOpsError):
    """Raised when a language model could not be reached or gave an unusable reply.

    The message never contains the request, the reply body or any credentials.
    """


class InvalidLLMOutput(LLMError):
    """Raised when a model's reply is not a diagnosis HealOps is willing to use."""