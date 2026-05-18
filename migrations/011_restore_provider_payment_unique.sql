-- Migration: Restore uniqueness on (provider, provider_payment_id) for automated providers.
--
-- 005 dropped the unique constraint outright because manual crypto payments
-- can legitimately reuse a TX hash across rows. That decision however left
-- automated providers (NowPayments, TetraPay, Tronado) with no DB-level
-- protection against webhook replay or duplicate inserts.
--
-- This restores a partial unique index scoped to the automated providers
-- only — manual rows are still allowed to duplicate.

CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_payment_id_automated
ON payments (provider, provider_payment_id)
WHERE provider_payment_id IS NOT NULL
  AND provider IN ('nowpayments', 'tetrapay', 'tronado');
