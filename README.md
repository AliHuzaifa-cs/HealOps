# HealOps

**Safe, model-agnostic self-healing for machine-learning applications.**

HealOps watches an ML application, detects when it breaks (a crash, a slow service, a
bad model, drifting data), asks a diagnosis agent what to do, checks the proposal against
strict policies, carries it out only if allowed, and verifies that the application really
recovered. People are asked whenever the policy says so.

> **Status: alpha (0.1.0).** The core loop is built and tested, but it has been exercised
> on a toy application, not yet on production systems. Read
> [docs/safety-and-limits.md](docs/safety-and-limits.md) before trusting it with anything
> that matters.

## The one rule

```
Monitor -> Detect -> Diagnose -> Plan -> Validate -> Act -> Verify -> Record
```

| Who | May do | May not do |
| --- | --- | --- |
| The **LLM** (or the rule-based agent) | propose a diagnosis and one action | change anything |
| The **policy engine** | allow, deny, or require a person's approval | invent actions |
| Your **adapter** | execute an allowed action | decide what to run |
| The **verifier** | confirm recovery from observed evidence | take the agent's word for it |

The language model is never trusted: its output is validated, it can only choose from
actions your application really supports, its confidence is capped, and if it is down or
over budget a deterministic rule-based agent takes over. Everything is written to an
append-only audit log.

## Install

```
pip install healops
```

From a source checkout: `pip install -e .` (add `".[dev]"` for the tests and `ruff`).
Python 3.11 or newer.

## Five-minute quickstart

HealOps ships a tiny fake model service so you can see the whole loop without any setup.

```python
from healops import HealOps
from healops.testing.toy_app import COMPONENT, Fault, ToyApp

config = {
    "dry_run": False,  # let HealOps really act (the default is a safe dry-run)
    "audit_log_path": "quickstart_audit.jsonl",
    "state_db_path": "quickstart_state.db",
    "check_interval_seconds": 1,
    "min_confidence_for_auto": 0.3,
    "thresholds": {"latency_ms": 500, "error_rate": 0.05, "consecutive_breaches": 2},
    "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
    "policies": {"restart_component": {"verdict": "allow", "cooldown_seconds": 0}},
}

app = ToyApp()
healops = HealOps(config)
healops.register("toy-app", app, component=COMPONENT)

app.inject(Fault.CRASH)
print("healthy before:", app.get_health())
healops.run(max_ticks=6)
print("healthy after: ", app.get_health())
print("actions taken: ", app.actions)
```

Expected output:

```
healthy before: False
healthy after:  True
actions taken:  ['restart_component:model-server']
```

The same file is [examples/quickstart.py](examples/quickstart.py). The run leaves
`quickstart_state.db` and `quickstart_audit.jsonl` in the current folder (delete them any time).

## Connect your own application

You write a small **adapter**: a class with only the methods your application really has
(`get_health`, `get_metrics`, `restart_component`, ...). HealOps discovers what it can do and
never calls anything else. See [docs/adapter-guide.md](docs/adapter-guide.md).

```python
healops = HealOps.from_config("selfhealing.yaml")
healops.register("my-model-api", MyAdapter(), component="model-server")
healops.run()   # until healops.stop() is called
```

All settings live in one YAML file, with safe defaults:
[docs/configuration.md](docs/configuration.md) and
[examples/selfhealing.example.yaml](examples/selfhealing.example.yaml).

## Which language model?

None is required: without an `llm:` section a rule-based agent is used. To use a model, add
a few lines of configuration; no code changes, and any provider that speaks the OpenAI
chat format works (OpenAI, Ollama, Gemini's compatibility endpoint, ...) plus Anthropic.

```yaml
llm:
  provider: ollama           # ollama | openai | anthropic | openai_compatible
  model: llama3.2:3b
  # api_key_env: OPENAI_API_KEY   # the NAME of an environment variable, never the key
```

API keys are read from environment variables only. A per-hour call budget
(`max_calls_per_hour`, default 20) bounds cost, and when the model fails or the budget is
used up, the rule-based agent answers with its confidence capped, so a person decides.

## Approving from another terminal

When the policy needs a person, HealOps parks the incident and alerts you (console, the
state store and, if configured, email). Answer from any terminal:

```
healops list
healops approve 3f9a1c2e --by ali
healops reject  3f9a1c2e --by ali --reason "not now"
```

The command only records your answer in the state file; the running HealOps carries it out
through the same policy, audit and verification path. An approval that expired is never
applied. (`python -m healops ...` does the same.)

## What is in the box

| Area | What it does |
| --- | --- |
| Detection | thresholds that must hold for several checks in a row; model-quality degradation against a baseline |
| Diagnosis | rule-based agent; LLM agent with strict JSON validation; automatic fallback; call budget |
| Policy | allow / needs approval / deny per action, retry limits, cooldowns, minimum confidence |
| Approval | human-in-the-loop queue that survives restarts; silence is never approval |
| Safety | dry-run by default; time limits on every adapter call; write-ahead audit before real actions |
| Verification | re-checks health and the incident's own metric after the action |
| State | SQLite file with observations, incidents, approvals and alerts (for dashboards) |
| Alerts | console, state store, email with rate limits |

## Documentation

- [Adapter guide](docs/adapter-guide.md): connect your application
- [Configuration reference](docs/configuration.md): every option and its default
- [Safety and known limits](docs/safety-and-limits.md): what is guaranteed and what is not

## Authors and acknowledgements

HealOps is the final-year project "Agentic Self-Healing MLOps".

- Ali Huzaifa (maintainer)
- Muhammad Rafeh
- Sufyan Khan
- Maaz Tariq Soomro
- Supervisor: Sir Abdullah Bin Tahir

Thanks to everyone whose feedback shaped the design.

## License

MIT. See [LICENSE](LICENSE).