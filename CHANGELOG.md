# Changelog

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
