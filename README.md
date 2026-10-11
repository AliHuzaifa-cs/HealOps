# HealOps

HealOps is a Python framework that watches a machine learning application,
detects failures, asks an LLM-based agent to propose a recovery action, checks
that action against deterministic safety policies, runs it only if allowed, and
verifies whether the application really recovered.

> **Status: early development (Level 1, foundation).** Only the core
> vocabulary, configuration and application registration exist so far.
> Detection, the diagnosis agent, the policy engine, the orchestrator and
> recovery verification are not built yet. Nothing here is production-ready.

## The idea

Monitor -> Detect -> Diagnose -> Plan -> Validate -> Act -> Verify -> Record

The key rule is separation of authority:

- the LLM only **proposes**,
- a deterministic policy engine **permits**,
- the application's own adapter **executes**,
- a verifier **confirms** from observable evidence.

## What exists today

| Module | Purpose |
| --- | --- |
| `types.py` | Shared enums: incident states, action types, policy verdicts, outcomes |
| `events.py` | `Observation` and `Incident` records |
| `records.py` | `Diagnosis` and `ActionProposal` records |
| `outcomes.py` | `PolicyDecision`, `ActionResult`, `VerificationResult` records |
| `lifecycle.py` | Allowed incident state transitions |
| `interfaces.py` | Eight small capability interfaces an application adapter can implement |
| `capabilities.py` | Discovers what an adapter can do and which actions are possible |
| `registry.py` | Registers applications by name |
| `config.py`, `loader.py` | Validated YAML configuration with safe defaults |
| `serialization.py` | JSON round trip for records, validated on the way in |
| `redaction.py` | Best-effort hiding of secrets before logging |

## Install

Python 3.11 or newer is required.

    python -m venv .venv
    .venv\Scripts\Activate.ps1        # Windows PowerShell
    source .venv/bin/activate         # macOS / Linux
    pip install -e ".[dev]"

## Run the tests

    pytest
    ruff check src tests

## Example

An adapter implements only the capabilities its application really has:

    from healops.registry import ApplicationRegistry

    class MyAdapter:
        def get_health(self) -> bool:
            return True

        def reload_model(self) -> str:
            return "model reloaded"

    registry = ApplicationRegistry()
    app = registry.register("my-app", MyAdapter())
    print(app.capabilities.names())
    # ('HealthProvider', 'ModelReloader')
    print(sorted(a.value for a in app.capabilities.supported_actions()))
    # ['escalate', 'no_action', 'reload_model', 'run_diagnostic']

Configuration is a YAML file; see `examples/selfhealing.example.yaml`.
With no settings at all, HealOps defaults to `dry_run: true`, so proposed
actions are only logged.

## Safety principles

- Actions come from an explicit allowlist (`ActionType`); anything else is rejected.
- An agent may not name a cause without citing evidence, and may abstain.
- `executed` is not `resolved`: only a verified recovery resolves an incident.
- Redeployment can never be fully automatic.
- Secret redaction is best-effort, not a guarantee.

## Project layout

    src/healops/    the framework
    tests/          automated tests
    examples/       example configuration