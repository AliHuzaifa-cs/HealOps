# Changelog

## 0.4.0
- `docker` config section: HealOps restarts allow-listed docker compose services through the Docker socket (`healops.adapters.docker`). Your adapter no longer needs any Docker code.
- `playbook` and `restart_targets` config: choose the actions tried per incident type and aim a restart at the right component.
- `policies.<action>.min_confidence`: a per-action confidence limit for safe, undoable actions such as a restart.
- `llm.enforce_playbook` (default `true`): a model answer outside the playbook is refused and the rules answer instead.

## 0.3.3
- Dashboard pages: Drift (checks, 14-day heat map, charts), Models, Agent & Policies, Integrations.
- Approve / reject / mark resolved from a Docker dashboard with `HEALOPS_ACTION_TOKEN` or `--action-token`.
- State file is backed up automatically (`state_backup_hours`, `state_backup_keep`) and restored if damaged.
- Per-feature PSI shown from `feature_psi.<feature>` metrics; incident page names the suspect feature.
- CI workflow, lint clean.

## 0.3.2
- Automatic recovery checks (`recovery_checks`) and `healops resolve`.

## 0.3.1
- Dashboard fixes and design polish.

## 0.3.0
- Read-only web dashboard (`healops dashboard`, `healops demo`).
