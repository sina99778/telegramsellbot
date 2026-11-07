# Renewal cycle display repair

**Goal:** A renewed 20 GiB plan displays 20 GiB and consumption from the start of that renewal, while cumulative panel accounting stays replay-safe.
**Approved approach:** The user requested the fix after diagnosis of the reported service. Store a nullable lifetime-usage baseline at full-plan renewal. Display usage as lifetime + current counter minus baseline; finite displayed quota is cumulative panel cap + lifetime minus baseline. Preserve existing display when historical baseline is unknown. Volume/time additions retain the baseline. Migration preserves it while rolling old client usage into lifetime.

- [ ] Add failing pure usage and renewal tests; add nullable cycle_start_used_bytes plus migration009; set baseline transactionally during full-plan renewal only.
- [ ] Update bot service detail, refresh, banner and miniapp subscription projections with the shared display helper; retain raw values for quota writes, billing totals and accounting.
- [ ] Update renewal sales messages and low-volume warnings; test 20+20 cumulative case, repeated reset, extra volume, unlimited quota and migration.
- [ ] Run focused/full tests, real PostgreSQL transaction checks, UI build/tests, quality debt gates and independent review; never baseline new errors.
- [ ] Push/merge verified changes and deploy with backup/image retention under the existing authorization. Backfill the confirmed reported service's display baseline only under row lock with original renewal timestamp and quota checks; never modify its panel quota, consumption, expiry or wallet.

**Review focus:** Legacy null baseline; cumulative lifetime reporting; delayed panel usage; new-client migration; idempotent renewal retry; rollback of financial/usage metadata; banner and notification consistency.
