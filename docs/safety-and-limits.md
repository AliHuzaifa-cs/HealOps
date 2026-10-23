# Safety and known limits

## What HealOps guarantees (and how it is tested)

- **Dry-run by default.** Nothing is executed until you set `dry_run: false`.
- **The model cannot act.** The agent only returns a diagnosis and one proposed action. The
  policy engine decides; your adapter executes; the verifier checks.
- **The model's output is not trusted.** It must be valid JSON; the action must be one that
  is offered and possible for this application; evidence must name readings that were
  really supplied; the component name comes from your registration, not from the model;
  confidence is capped.
- **A person can always be asked.** Anything unclear, denied, failed or unverified is
  escalated. Silence is never approval, and an expired approval is never applied.
- **Bounded retries.** A failed or timed-out action is not repeated by itself; retries and
  cooldowns come from the policy.
- **Write-ahead audit.** Before a real action runs, its intent is written to the audit log.
  If that write fails, the action does not run.
- **No raw secrets in records.** API keys and passwords are read from environment variables
  at the moment of use; well-known secret shapes are hidden before anything is logged or
  emailed.
- **A crash never repeats an action.** After a restart, an incident that was being handled is
  escalated instead of executed again.
- **No LLM needed.** If the model is unreachable, slow, over budget or wrong, a deterministic
  rule-based agent takes over.

These are checked by the test suite, which includes deliberately broken versions of the code
to confirm that the tests really fail when a safeguard is removed.

## Known limits (alpha)

- **Tested mainly on a toy application.** The loop has been run end-to-end on the bundled
  toy service and with real language models on it, not on production traffic. The rule-based
  and language-model prompts were tuned on those same toy faults, so measure them again on
  your own application.
- **One incident at a time, no correlation.** Several symptoms of one root cause are handled
  as separate incidents.
- **`run_diagnostic` is not implemented.** The agent can name it, but nothing collects extra
  evidence yet, so it is not offered to language models.
- **Approvals are not authenticated.** `healops approve --by NAME` records a name, not an
  identity. Whoever can write the state file can answer approvals; protect that file like the
  application. (A dashboard with login tokens is planned.)
- **One writer per state file.** Run one HealOps process per state file.
- **A timed-out adapter call cannot be killed.** Python cannot stop a thread, so stuck calls
  are counted and further calls are refused after `max_stuck_calls`. Adapter methods must be
  thread-safe.
- **Memory of past actions is per process.** Retry limits and cooldowns restart from zero
  after a restart (waiting approvals and open incidents do survive).
- **Email has been tested against a mock SMTP server.** Run
  `examples/live_email_check.py` against your own account before relying on it.
- **Language-model latency.** A local model can take tens of seconds per diagnosis; the
  monitoring loop waits for it.
- **Not a security product.** HealOps recovers ML applications; it does not defend them.

## Reporting a problem

Open an issue with your configuration (with secrets removed), the relevant lines of the audit
log and the output of `healops list`.