# Whole-Codebase Review Report

## Executive summary

The repository is a self-hosted Telegram VPN/proxy sales platform composed of a Python/FastAPI API, Aiogram bot, APScheduler worker, PostgreSQL/Redis services, Vue dashboard, static Telegram Mini App, payment integrations, and panel provisioning clients.

The existing automated suite is healthy: **656 tests passed**. The dashboard type-check and production build also passed. The review nevertheless found several concrete risks that are not fully covered by the current tests. The most urgent issue is a possible **duplicate wallet refund after automatic provisioning failure**. Other high-priority issues affect payment concurrency, provider-side duplicate provisioning, replay protection migrations, and backup secret exposure.

This report is a review and prioritization document. No fixes were applied as part of this whole-codebase review.

## Verification performed

- `python -m pytest -q`: **656 passed**
- Dashboard `npm run build`: **passed** (`vue-tsc --noEmit && vite build`)
- `ruff check .`: unavailable; Ruff is not installed
- `mypy .`: unavailable; mypy is not installed

The worktree already contained changes and untracked files from earlier work; those were not modified or removed.

## Findings summary

| ID | Severity | Area | Finding |
|---|---|---|---|
| PAY-001 | Critical | Payments | Automatic provisioning refund does not set `payment_status` to `refunded`, allowing a second manual refund |
| PAY-002 | High | Payments | Administrative “mark failed” can race with webhook/worker processing |
| PAY-003 | High | PasarGuard | Non-idempotent user creation can be retried after an uncertain timeout |
| PAY-004 | High | X-UI provisioning | Failed compensating cleanup can leave orphaned remote clients that later retries duplicate |
| PAY-005 | High | Migrations | Duplicate payment keys cause the replay-protection unique index to be skipped while migration still succeeds |
| SEC-001 | High | Backups | Routine backups contain live secrets in unencrypted archives |
| SEC-002 | High | Dashboard auth | Sliding 14-day bearer sessions have no server-side revocation or absolute lifetime |
| SEC-003 | Medium | SSRF | Server-side subscription fetching follows redirects and disables TLS verification |
| SEC-004 | Medium | Webhooks | TetraPay unsigned callback compatibility is enabled by default |
| SEC-005 | Medium | Logs/files | Sensitive values can be logged; receipt downloads lack strict size/type limits |
| API-001 | Medium | Recovery | Resend-config recovery can send the wrong subscription |
| API-002 | Medium | Dashboard | Invalid date filters are silently ignored |
| UI-001 | Medium | Dashboard | Concurrent refresh requests can overwrite newer state with stale responses |
| API-003 | Low/Medium | Analytics | Overview and analytics use inconsistent revenue status definitions |
| OPS-001 | Medium | CI/build | CI does not run lint, type-check, dashboard build, Docker build, or integration checks |
| OPS-002 | Medium | Dependencies | Docker dependency declarations can drift from project metadata |

## Critical findings

### PAY-001 — Automatic provisioning refund may be duplicated

**Impact:** A user can receive the same wallet refund twice, causing direct financial loss and ledger inconsistency.

In [`services/payment.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/payment.py#L487-L530), automatic refund processing credits the wallet and sets the order to `refunded`, but the shown success branch updates payload flags without setting `payment.payment_status = "refunded"`. The manual recovery handler prevents duplicate refunds primarily by checking that status in [`recovery.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/bot/handlers/admin/recovery.py#L559-L581).

Likely sequence:

1. Payment is credited.
2. Provisioning fails.
3. Automatic wallet refund succeeds.
4. Payment remains `finished` or another non-refunded status.
5. Recovery UI offers manual refund.
6. Manual refund credits the same amount again.

**Recommended fix:** Make refunding an atomic, durable operation. Set the payment refund state in the same transaction as the refund ledger entry, and enforce one refund per payment with a database uniqueness constraint or a locked existence check. Apply the same audit to the automatic renewal-refund branch around [`services/payment.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/payment.py#L711-L749).

## High-priority findings

### PAY-002 — Administrative “mark failed” race

[`recovery.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/bot/handlers/admin/recovery.py#L606-L635) loads a payment without `FOR UPDATE`, then writes `payment_status = "failed"`. A webhook or reconciliation worker can process and credit the same payment between the read and commit, allowing the stale admin write to overwrite a successful result.

**Recommended fix:** Lock the payment row, then re-check actual payment and fulfillment/refund markers before allowing the transition. Reject the action if processing has started or completed.

### PAY-003 — PasarGuard create-user retry can duplicate remote users

The request layer in [`services/pasarguard/client.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/pasarguard/client.py#L265-L305) retries transport failures for all methods, while user creation calls `POST /api/user` in [`services/pasarguard/client.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/pasarguard/client.py#L153-L157). A timeout after the remote commit but before the response can create a second user.

**Recommended fix:** Do not blindly retry uncertain non-idempotent creates. Reconcile by deterministic username/ID before retrying, or use provider idempotency support if available.

### PAY-004 — X-UI orphan cleanup is not durable

The X-UI provisioning flow attempts compensating deletion after uncertain `addClient` outcomes in [`services/provisioning/manager.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/provisioning/manager.py#L500-L523). If cleanup fails, the local transaction rolls back and no durable record identifies the possible remote client. A later retry creates a new identity, leaving the first client orphaned.

**Recommended fix:** Persist an uncertain-provisioning record containing panel, inbound, and remote client identity, and reconcile it before retrying. A deterministic remote identity can also make lookup-before-create safe.

### PAY-005 — Replay-protection index may be absent while deployment succeeds

[`005_money_constraints_and_payment_unique.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/scripts/migrations/005_money_constraints_and_payment_unique.py#L131-L164) returns a duplicate status instead of creating the unique payment index. Its main function then exits zero at [`005_money_constraints_and_payment_unique.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/scripts/migrations/005_money_constraints_and_payment_unique.py#L200-L215).

**Recommended fix:** Reconcile duplicates and create the index, or fail deployment clearly when the replay-defense index cannot be installed. Do not report a successful security migration while the defense is absent.

### SEC-001 — Backups contain live secrets without encryption

The worker backup code includes environment secrets in the archive in [`backup.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/worker/jobs/backup.py#L332-L367) and writes/sends the archive in [`backup.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/worker/jobs/backup.py#L463-L506). The shell backup path similarly includes `.env` in [`backup.sh`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/backup.sh#L81-L87).

**Impact:** A leaked archive exposes bot tokens, encryption keys, database/Redis credentials, provider secrets, panel credentials, and customer data.

**Recommended fix:** Exclude secrets from routine backups, or encrypt the complete archive with a separately managed key. Restrict permissions, retention, and delivery destinations. Rotate secrets if archives were exposed.

### SEC-002 — Dashboard sessions lack server-side revocation

Signed sessions contain an admin identity and expiry in [`dashboard_auth.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/core/dashboard_auth.py#L100-L155). Authenticated requests issue a new 14-day token in [`_deps.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/_deps.py#L44-L63). Logout only deletes the browser cookie in [`auth.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/auth.py#L161-L171).

**Impact:** A stolen cookie can remain usable and be kept alive by activity. Browser logout does not revoke a copied token.

**Recommended fix:** Use server-side opaque sessions, or add a per-admin session version/auth epoch and an absolute maximum lifetime. Increment the version on password changes, disablement, and logout-all.

## Medium-priority findings

### SEC-003 — Subscription fetching creates an SSRF surface

[`services/provisioning/manager.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/provisioning/manager.py#L1909-L1942) fetches a database-selected URL with redirects enabled and TLS verification disabled.

**Recommended fix:** Validate initial and redirected hosts/IPs, reject loopback/private/link-local/metadata addresses, bound redirects, and keep certificate verification enabled. Avoid logging full subscription URLs.

### SEC-004 — TetraPay unsigned callbacks enabled by default

The default compatibility flag is enabled in [`core/config.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/core/config.py#L119-L124), and the webhook accepts missing/invalid signatures under that flag in [`tetrapay.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/webhooks/tetrapay.py#L69-L95).

Provider-side verification reduces the immediate risk, but the safer default is strict signature validation. Change the default to false and enable legacy mode only explicitly during a controlled migration.

### SEC-005 — Sensitive logs and unrestricted receipt media

Raw TetraPay payloads are logged in [`tetrapay.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/webhooks/tetrapay.py#L34-L60). Generated dashboard passwords are logged in [`scripts/dashboard_admin.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/scripts/dashboard_admin.py#L98-L100). Subscription URLs can be logged in [`services/provisioning/manager.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/provisioning/manager.py#L1934-L1936).

Receipt proxying in [`receipts.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/receipts.py#L203-L215) reflects upstream content type and buffers the whole response without strict size/type validation.

**Recommended fix:** Allowlist and redact log fields; never log passwords or bearer-like subscription links. Enforce maximum download size, verify image signatures/dimensions, return only safe image media types, and set `X-Content-Type-Options: nosniff`.

### API-001 — Recovery resend can select the wrong subscription

The payment payload records a subscription ID in [`services/payment.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/services/payment.py#L418-L422), but recovery selects the newest active subscription for the same user and plan in [`recovery.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/bot/handlers/admin/recovery.py#L484-L499).

**Recommended fix:** Resolve by recorded `subscription_id` or a direct payment/order relation. Use user+plan fallback only with explicit admin confirmation.

### API-002 — Invalid date filters are silently ignored

[`transactions.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/transactions.py#L54-L60) returns `None` for invalid dates, and filter predicates are skipped in [`transactions.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/transactions.py#L94-L101). CSV export repeats this behavior around [`transactions.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/transactions.py#L149-L158).

**Recommended fix:** Return `400` for malformed supplied dates and define inclusive date-only semantics explicitly.

### UI-001 — Stale asynchronous dashboard responses

Views such as [`Users.vue`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/dashboard/src/views/Users.vue#L24-L60), [`Transactions.vue`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/dashboard/src/views/Transactions.vue#L47-L109), and [`Receipts.vue`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/dashboard/src/views/Receipts.vue#L34-L52) apply responses without request sequencing or cancellation.

**Impact:** A slow older request can overwrite data for a newer filter/tab/page.

**Recommended fix:** Use request IDs or `AbortController` and only commit the latest response.

### API-003 — Inconsistent revenue definitions

Overview excludes `completed` orders in [`overview.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/overview.py#L15-L19), while analytics includes it in [`analytics.py`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/apps/api/routes/dashboard/analytics.py#L7-L10).

**Recommended fix:** Centralize revenue statuses or label the two metrics as intentionally different.

### OPS-001 — CI does not validate the full product

[`ci.yml`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/.github/workflows/ci.yml#L9-L27) runs Python tests but not Ruff, mypy, the dashboard build, Docker build, Compose validation, or PostgreSQL/Redis integration tests.

**Recommended fix:** Add staged CI jobs for lint/type-check, dashboard build, Docker/Compose validation, and integration tests.

### OPS-002 — Dependency definitions can drift

The Dockerfile maintains a separate hand-written Python dependency list in [`Dockerfile`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/Dockerfile#L17-L47), and uses `npm install` despite a lockfile at [`dashboard/package-lock.json`](file:///C:/Users/sinan_heded4z/.gemini/antigravity/scratch/telegramsellbot/dashboard/package-lock.json). This can make production differ from local/CI environments.

**Recommended fix:** Use one dependency source of truth and `npm ci` in Docker. Add a CI check for dependency drift.

## Four-perspective synthesis

### Senior software engineer

The highest architectural issue is duplicated state-transition logic across payment, wallet, order, subscription, and recovery paths. Refunds and fulfillment should be modeled as explicit idempotent state machines with durable relations and database constraints, rather than inferred from loosely related status and JSON payload flags.

### Performance-focused systems programmer

The current snapshot-based X-UI synchronization is efficient, but dashboard refresh races, unbounded receipt buffering, repeated provider retries, and missing integration checks can create latency spikes or worker starvation. Prefer bounded I/O, cancellation, bounded retries, and one shared state-update path.

### Junior developer prioritizing simplicity

The safest first fixes are small and testable: set the payment to `refunded` after a successful automatic refund, lock and validate before “mark failed,” stop retrying PasarGuard creates, use the recorded subscription ID for resend, change TetraPay’s default to strict mode, and use `npm ci`.

### Security-focused engineer

Protect secrets and money first: encrypt/remove secrets from backups, prevent duplicate refunds, require signed webhooks by default, harden server-side URL fetching against SSRF, add session revocation, redact logs, and enforce file size/type validation.

## Recommended implementation order

1. PAY-001: make automatic refunds durable and idempotent.
2. PAY-002: lock and validate administrative payment state transitions.
3. PAY-003 and PAY-004: reconcile uncertain provider mutations and prevent duplicate remote provisioning.
4. PAY-005: make replay-protection migration fail closed or reconcile duplicates.
5. SEC-001: remove or encrypt secrets in backups.
6. SEC-003/SEC-004/SEC-005: harden SSRF, webhook signatures, logs, and receipt media.
7. API-001, API-002, UI-001, API-003: fix recovery identity, validation, stale UI state, and metric consistency.
8. OPS-001/OPS-002: strengthen CI and dependency reproducibility.

## Review conclusion

The codebase has good foundations: parameterized SQLAlchemy access, signed dashboard sessions, password hashing, wallet row locking, provider idempotency checks in several paths, X-UI mutation retry restrictions, and broad Python test coverage. The failures above are concentrated at cross-system boundaries where local database transactions cannot automatically roll back external provider actions. Those boundaries should receive explicit durable idempotency, reconciliation, and security controls before adding more features.
