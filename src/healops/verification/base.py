"""The contract for checking that a recovery action really worked."""

from typing import Protocol

from healops.events import Incident
from healops.outcomes import ActionResult, VerificationResult
from healops.registry import RegisteredApplication
from healops.types import VerificationOutcome


class Verifier(Protocol):
    """Looks at the application after an action and says whether it recovered.

    It must use observable evidence (health, metrics), never the fact that the
    action's callback returned without an error.
    """

    def verify(
        self,
        application: RegisteredApplication,
        incident: Incident,
        result: ActionResult,
    ) -> VerificationResult: ...


class NoVerifier:
    """The safe default: without a real verifier recovery can never be proven."""

    def verify(
        self,
        application: RegisteredApplication,
        incident: Incident,
        result: ActionResult,
    ) -> VerificationResult:
        return VerificationResult(
            execution_id=result.execution_id,
            outcome=VerificationOutcome.INCONCLUSIVE,
        )