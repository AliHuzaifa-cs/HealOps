"""A safety net for the diagnosis agent: if the main agent fails, rules take over.

When the primary agent (for example an LLM) crashes or is unreachable, the
fallback agent (usually the rule-based MockDiagnosisAgent) diagnoses instead.
The result is clearly marked as a fallback and its confidence is capped low, so
the policy engine's confidence gate sends it to a person rather than acting alone.
"""

from dataclasses import replace

from healops.agents.base import AgentResult, DiagnosisAgent, DiagnosisContext

DEFAULT_CONFIDENCE_CAP = 0.2


class FallbackAgent:
    """Tries the primary agent; on any failure, uses the fallback agent instead.

    Only the error type is recorded, never the error message, because an
    exception message from a provider can contain secrets. If the fallback agent
    fails too, its error propagates and the orchestrator escalates.
    """

    def __init__(
        self,
        primary: DiagnosisAgent,
        fallback: DiagnosisAgent,
        confidence_cap: float = DEFAULT_CONFIDENCE_CAP,
    ) -> None:
        if not (0.0 <= confidence_cap <= 1.0):
            raise ValueError("confidence_cap must be between 0.0 and 1.0")
        self._primary = primary
        self._fallback = fallback
        self._cap = confidence_cap
        self.fallback_count = 0
        self.last_error_type: str | None = None

    def diagnose(self, context: DiagnosisContext) -> AgentResult:
        try:
            return self._primary.diagnose(context)
        except Exception as error:  # noqa: BLE001 - any primary failure triggers the fallback
            self.fallback_count += 1
            self.last_error_type = type(error).__name__

        result = self._fallback.diagnose(context)
        diagnosis = replace(
            result.diagnosis,
            summary=(
                f"[FALLBACK: primary agent unavailable ({self.last_error_type})] "
                f"{result.diagnosis.summary}"
            ),
            confidence=min(result.diagnosis.confidence, self._cap),
        )
        proposal = result.proposal
        if proposal is not None:
            proposal = replace(proposal, diagnosis_id=diagnosis.diagnosis_id)
        return AgentResult(diagnosis, proposal)