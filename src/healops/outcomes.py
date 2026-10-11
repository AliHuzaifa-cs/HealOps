"""Records for decisions and results: PolicyDecision, ActionResult, VerificationResult."""

from dataclasses import dataclass, field
from datetime import datetime

from healops.events import _new_id, _now, _require_aware, _require_text
from healops.types import PolicyVerdict, VerificationOutcome


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The policy engine's verdict on one ActionProposal, with its reasons."""

    proposal_id: str
    verdict: PolicyVerdict
    reasons: tuple[str, ...]
    decision_id: str = field(default_factory=_new_id)
    decided_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        _require_aware(self.decided_at, "decided_at")
        if not isinstance(self.verdict, PolicyVerdict):
            raise TypeError(f"verdict must be a PolicyVerdict, got {self.verdict!r}")
        if not self.reasons:
            raise ValueError("a policy decision must state at least one reason")


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What happened when an action was run (or dry-run).

    `completed` only says the callback finished without raising. It is NOT proof
    that the application recovered; that is the verifier's job.
    """

    proposal_id: str
    dry_run: bool
    completed: bool
    message: str
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime = field(default_factory=_now)
    execution_id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        _require_text(self.message, "message")
        _require_aware(self.started_at, "started_at")
        _require_aware(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot be earlier than started_at")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Whether the application really recovered, judged from observable checks."""

    execution_id: str
    outcome: VerificationOutcome
    checks_passed: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    verification_id: str = field(default_factory=_new_id)
    verified_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id")
        _require_aware(self.verified_at, "verified_at")
        if not isinstance(self.outcome, VerificationOutcome):
            raise TypeError(f"outcome must be a VerificationOutcome, got {self.outcome!r}")
        if self.outcome is VerificationOutcome.VERIFIED and (
            not self.checks_passed or self.checks_failed
        ):
            raise ValueError("VERIFIED needs passed checks and no failed checks")
        if self.outcome is VerificationOutcome.FAILED and not self.checks_failed:
            raise ValueError("FAILED needs at least one failed check")
        if self.outcome is VerificationOutcome.PARTIAL and not (
            self.checks_passed and self.checks_failed
        ):
            raise ValueError("PARTIAL needs both passed and failed checks")