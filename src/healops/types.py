"""Shared vocabulary (enums) used across HealOps."""

from enum import StrEnum


class ObservationKind(StrEnum):
    HEALTH = "health"
    METRIC = "metric"
    DRIFT = "drift"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentType(StrEnum):
    HEALTH_FAILURE = "health_failure"
    HIGH_LATENCY = "high_latency"
    HIGH_ERROR_RATE = "high_error_rate"
    DATA_DRIFT = "data_drift"
    DATA_QUALITY = "data_quality"
    PERFORMANCE_DEGRADATION = "performance_degradation"
    PREDICTION_ANOMALY = "prediction_anomaly"


class IncidentState(StrEnum):
    OPEN = "open"
    DIAGNOSED = "diagnosed"
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class ActionType(StrEnum):
    RESTART_COMPONENT = "restart_component"
    RELOAD_MODEL = "reload_model"
    ROLLBACK_MODEL = "rollback_model"
    REQUEST_RETRAINING = "request_retraining"
    REQUEST_REDEPLOYMENT = "request_redeployment"
    RUN_DIAGNOSTIC = "run_diagnostic"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyVerdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class VerificationOutcome(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"
    ESCALATION_REQUIRED = "escalation_required"