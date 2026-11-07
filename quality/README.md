# Existing lint and type debt

CI runs `python scripts/check_quality.py ruff` and `python scripts/check_quality.py mypy`.
These commands compare diagnostics against the explicitly recorded JSON baselines;
a green gate means no new findings, not that the repository is lint/type clean.

The scope is `apps`, `core`, `models`, `repositories`, `schemas`, `services`, plus
the gate and its tests. Tool versions are pinned in `pyproject.toml`. The gate
and its tests must have zero findings and cannot add themselves to the baseline.

Each JSON key records relative path, diagnostic code, full message, and SHA-256
of the exact source line. Counts prevent an identical additional finding from
hiding behind old debt. Line numbers are omitted so moving unchanged code does
not create noise. Removed findings are reported; prune their baseline entries
so they cannot be reintroduced later under a stale allowance.

To deliberately review and record existing debt, run either command with
`--write-baseline`, inspect the JSON diff, and commit it with the explanation.
Never refresh baselines merely to make a failing change green. No ignore rules
are added by this gate. Tool crashes, unexpected stderr, malformed diagnostics,
invalid locations, and mismatched exit statuses fail the gate.

The initial November 7, 2026 baseline records the existing debt identified in
`docs/deep-audit-2026-11-07-fa.md`; it does not assert those findings are fixed.
Dependency updates may change mypy results and require review even when the
checker version stays pinned. Real PostgreSQL/panel validation is independent.

Baseline provenance was checked against archived revision `025b36e`, using the
same pinned tools, current gate/test files and current tool configuration. With
mypy `--explicit-package-bases`, that revision reports 512 errors (the earlier
audit's direct invocation reported 506). These counts describe different module
mapping modes; neither is a claim of repaired type debt. New repair diagnostics
are reviewed against that revision before inclusion is allowed.

The final repair comparison removed six Ruff findings and introduced none.
Two existing mypy fingerprints were reviewed and moved without increasing
error counts: `_apply_renewal` remains an existing untyped subscription parameter
whose signature now spans multiple lines; `services/payment.py` retains its
existing `bot` redefinition diagnostic, whose message embeds the shifted earlier
line number. These are recorded as existing debt, not newly accepted errors.

The renewal-cycle display repair removed one further Ruff finding and typed the
existing `_days_label` helper, removing one mypy finding. Baselines were pruned
to 1774 and 511 respectively after both gates confirmed zero new diagnostics.
