"""A tiny fake model service for demos and end-to-end tests.

It lives in memory, has no randomness and needs no network or model file, so
every run behaves the same. You inject a fault, and only the matching cure
fixes it, so a test can check that HealOps picks the right action:

    fault            what you see                         what fixes it
    ---------------  -----------------------------------  ----------------------
    CRASH            health False                         restart_component
    SLOW             latency_ms 900                       restart_component
    MODEL_CORRUPT    health False                         reload_model
    BAD_MODEL        error_rate 0.20 and f1_score 0.62    rollback_model
    DRIFT            drift score 0.80                     nothing automatic

DRIFT has no cure on purpose: retraining is requested but never deployed by
itself, so the right outcome is that a person is asked.
"""

from enum import StrEnum

COMPONENT = "model-server"

_NORMAL_LATENCY_MS = 100.0
_NORMAL_ERROR_RATE = 0.01
_NORMAL_F1 = 0.90
_NORMAL_DRIFT = 0.05

_SLOW_LATENCY_MS = 900.0
_BAD_ERROR_RATE = 0.20
_BAD_F1 = 0.62
_DRIFTED = 0.80


class Fault(StrEnum):
    CRASH = "crash"
    SLOW = "slow"
    MODEL_CORRUPT = "model_corrupt"
    BAD_MODEL = "bad_model"
    DRIFT = "drift"


class ToyApp:
    """An application adapter that implements every HealOps capability."""

    def __init__(self) -> None:
        self.faults: set[Fault] = set()
        self.actions: list[str] = []  # every action HealOps ran, in order

    # ----- fault injection (test side only) -----

    def inject(self, fault: Fault) -> None:
        if not isinstance(fault, Fault):
            raise TypeError(f"fault must be a Fault, got {fault!r}")
        self.faults.add(fault)

    def clear_all_faults(self) -> None:
        self.faults.clear()

    # ----- what HealOps can observe -----

    def get_health(self) -> bool:
        return not ({Fault.CRASH, Fault.MODEL_CORRUPT} & self.faults)

    def get_metrics(self) -> dict[str, float]:
        bad = Fault.BAD_MODEL in self.faults
        return {
            "latency_ms": _SLOW_LATENCY_MS if Fault.SLOW in self.faults else _NORMAL_LATENCY_MS,
            "error_rate": _BAD_ERROR_RATE if bad else _NORMAL_ERROR_RATE,
            "f1_score": _BAD_F1 if bad else _NORMAL_F1,
        }

    def get_drift_scores(self) -> dict[str, float]:
        drifted = Fault.DRIFT in self.faults
        return {"input_distribution": _DRIFTED if drifted else _NORMAL_DRIFT}

    # ----- what HealOps can do -----

    def restart_component(self, component: str) -> str:
        if component != COMPONENT:
            raise ValueError(f"unknown component {component!r}")
        self.actions.append(f"restart_component:{component}")
        self.faults -= {Fault.CRASH, Fault.SLOW}
        return f"restarted {component}"

    def reload_model(self) -> str:
        self.actions.append("reload_model")
        self.faults.discard(Fault.MODEL_CORRUPT)
        return "model reloaded"

    def rollback_model(self) -> str:
        self.actions.append("rollback_model")
        self.faults.discard(Fault.BAD_MODEL)
        return "rolled back to the previous model version"

    def request_retraining(self) -> str:
        self.actions.append("request_retraining")
        return "retraining requested (nothing is deployed automatically)"