# Configuration reference

HealOps reads one YAML file (`HealOps.from_config("selfhealing.yaml")`) or a Python
dictionary (`HealOps({...})`). Unknown keys are **rejected**, so a typo cannot silently
switch a check off. Every option has a safe default. A commented, ready-to-edit file is
[examples/selfhealing.example.yaml](../examples/selfhealing.example.yaml).

Secrets never go in this file: where a key or password is needed you write the **name of an
environment variable** (`api_key_env`, `password_env`) and set the variable in your shell.

## Top level

| Option | Default | Meaning |
| --- | --- | --- |
| `dry_run` | `true` | Log and alert proposed actions but never execute them. Turn off only after reading the audit log. |
| `check_interval_seconds` | `30` | Seconds between checks. |
| `min_confidence_for_auto` | `0.6` | An action the policy would allow automatically is downgraded to "needs approval" if the diagnosis confidence is below this (0 to 1). The gate only ever makes things stricter. |
| `approval_timeout_seconds` | `3600` | How long a person has to answer. After that the incident is escalated; silence is never approval. |
| `audit_log_path` | `healops_audit.jsonl` | Append-only record of every step. `null` keeps it in memory. |
| `state_db_path` | `healops_state.db` | SQLite file with observations, incidents, approvals and alerts. `null` keeps it in memory (nothing survives a restart). |
| `state_retention_days` | `30` | Observations older than this are deleted. Incidents are kept. |
| `max_recovery_attempts` | `3` | Recovery actions tried for one incident (1 to 10) before a person takes over. |
| `warn_unreported_after_checks` | `5` | Warn once about anything the config watches that the application never reported. `0` turns the warning off. |

## `thresholds`

A threshold left out is simply not monitored. A breach must repeat for
`consecutive_breaches` checks in a row before an incident is raised, so one odd reading does
not trigger anything.

| Option | Default | Meaning |
| --- | --- | --- |
| `latency_ms` | none | Incident `high_latency` when the metric `latency_ms` is above this. |
| `error_rate` | none | Incident `high_error_rate` when the metric `error_rate` is above this (0 to 1). |
| `drift_score` | none | Incident `data_drift` when any drift score is above this. |
| `consecutive_breaches` | `3` | Breaches in a row needed (1 to 20). |

A failing health check always raises a `health_failure` incident.

## `metric_degradation`

Model-quality metrics your application reports through `get_metrics()`, for example:

```yaml
metric_degradation:
  f1_score:
    baseline: 0.90      # the normal level
    max_change: 0.10    # how far it may move in the bad direction
    higher_is_better: true
```

| Option | Default | Meaning |
| --- | --- | --- |
| `baseline` | required | Normal value of the metric. |
| `max_change` | required | Allowed movement in the bad direction (greater than 0). |
| `higher_is_better` | `true` | `false` for metrics where lower is better (such as a loss). |

## `verification`

After an action HealOps watches the application before calling it recovered.

| Option | Default | Meaning |
| --- | --- | --- |
| `settle_seconds` | `10` | Wait before the first check. |
| `checks` | `3` | Number of checks (1 to 20). All must pass for "verified". |
| `interval_seconds` | `10` | Pause between checks. |
| `max_window_seconds` | `300` | The whole wait must fit inside this. |

## `default_policy` and `policies`

`policies` maps an action name to its rules; `default_policy` applies to every action not
listed. Actions: `restart_component`, `reload_model`, `rollback_model`, `request_retraining`,
`request_redeployment` (`run_diagnostic`, `escalate` and `no_action` are handled by HealOps).

| Option | Default | Meaning |
| --- | --- | --- |
| `verdict` | `needs_approval` | `allow` (run without asking), `needs_approval` (a person decides) or `deny` (never). |
| `max_retries` | `1` | Retries after the first run, per incident. |
| `cooldown_seconds` | `300` | Minimum time between runs of the same action on the same application. |

## `adapter_timeouts`

Every call into your adapter has a time limit. See the
[adapter guide](adapter-guide.md) for what happens on a timeout.

| Option | Default | Meaning |
| --- | --- | --- |
| `read_seconds` | `10` | Limit for `get_health`, `get_metrics`, `get_drift_scores`. |
| `action_seconds` | `120` | Limit for restart, reload, rollback and retraining calls. |
| `max_stuck_calls` | `3` | After this many calls to one application are stuck, no new call is made until one finishes. |

## `llm` (optional)

Leave the section out to use the rule-based agent only.

| Option | Default | Meaning |
| --- | --- | --- |
| `provider` | required | `ollama`, `openai`, `anthropic` or `openai_compatible`. |
| `model` | required | Model name as the provider knows it. |
| `base_url` | provider default | Required for `openai_compatible`. Ollama defaults to `http://localhost:11434`. |
| `api_key_env` | none | **Name** of the environment variable holding the key. Not needed for local Ollama. |
| `chat_path` | provider default | Path of the chat endpoint, for services with a different one (must start with `/`). |
| `json_mode` | `true` | Ask for a JSON reply; set `false` for services that reject it. |
| `timeout_seconds` | provider default | Per-call time limit. |
| `max_tokens` | `800` | Reply size limit. |
| `max_confidence` | `0.9` | Cap on the confidence the model may claim. |
| `max_calls_per_hour` | `20` | Budget for model calls. `null` removes the limit. |
| `use_fallback` | `true` | When the model fails or the budget is used up, the rule-based agent answers instead. |
| `fallback_confidence_cap` | `0.2` | Confidence cap for those fallback answers, so a person decides. |

## `email` (optional)

Alerts by SMTP when an action needs approval or an incident needs a person. The email only
informs; it contains the command to run, never a link. Held-back alerts are summarised in the
next email and always kept in the state store.

| Option | Default | Meaning |
| --- | --- | --- |
| `host` | required | SMTP server. |
| `port` | `587` | SMTP port. |
| `security` | `starttls` | `starttls`, `ssl` or `none` (`none` only for localhost). |
| `username` | none | Login name; needs `password_env`. |
| `password_env` | none | **Name** of the environment variable holding the password. |
| `sender` | required | From address. |
| `recipients` | required | One to twenty addresses. |
| `timeout_seconds` | `15` | SMTP connection limit. |
| `max_emails_per_hour` | `6` | Hourly limit. |
| `cooldown_minutes` | `15` | One email per application and incident type in this time. |
| `email_in_dry_run` | `false` | Email alerts that come from dry-run mode too. |
| `subject_prefix` | `[HealOps]` | Start of every subject line. |