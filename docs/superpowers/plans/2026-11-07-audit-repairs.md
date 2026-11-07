# Deep audit repairs implementation plan

**Goal:** Fix all six reproduced defects, restore useful quality gates, push and deploy verified code.
**Spec:** docs/deep-audit-2026-11-07-fa.md and the user's explicit instruction to fix all, push, and update the server.
**Architecture:** Keep existing wallet transactions and subscription outbox. Scope provider lookups, preserve identity-only patches, serialize migration correction and renewal, and select due outbox records before limiting batches.
**Constraints:** Preserve user files, no destructive database operations, no secrets in output or commits. Back up production before deployment. Use the current remote default branch as integration base.

## Tasks
- [ ] 1. Payments/identity: add failing regression tests for cross-provider NOW lookups and transfer limit preservation; fix lookup/patch boundaries; run targeted tests.
- [ ] 2. Migration: add regression for a renewed or pending migrated subscription; lock and re-read state, preserve purchased increments or reject unsafe historical correction, use durable outbox; run migration tests.
- [ ] 3. Renewal/outbox: add failing tests for terminal status after refresh, explicit unlimited modification, and due-row selection; implement and run focused tests.
- [ ] 4. Quality: fix safe mechanical lint errors, repair actual typing problems where practical, introduce explicit debt baseline only if needed without hiding new errors; validate baseline itself. Record remaining debt accurately.
- [ ] 5. Verify: full Python suite including disposable PostgreSQL tests when available, dashboard build/tests, dependency scans, independent review, fix important findings.
- [ ] 6. Publish/deploy: commit only task files, push, integrate into default branch if authorized/allowed, use production backup/update path, verify image revision, health, logs and outbox state; retain rollback image.

## Review focus
Cross-provider ID collisions and missing binding IDs; stale renewal snapshots after ban/ownership transfer; existing migration purchases; explicit null versus omitted fields; backoff fairness; transaction rollback and lock order; production image revision and database compatibility.

## Progress and rulings
- Baseline audit: 873 passed / 15 skipped, dashboard build and 25 UI tests pass; lint/type have existing debt.
- Ruling: user approved concrete audit fixes and execution, including push/deploy; no repeat approval needed for those steps.
- Ruling: use a new codex branch in the existing checkout; preserve all pre-existing untracked files. No additional worktree created.
- Remote advanced to 025b36e; fast-forward repair branch before implementation, preserving newer runtime fixes.
