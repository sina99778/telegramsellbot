-- Migration: Drop unique constraint on payments.provider_payment_id
-- Reason: Manual crypto payments use TX hash as provider_payment_id,
--         and duplicate hashes should not crash the system.

-- Drop the unique index (PostgreSQL names it automatically)
DO $$
BEGIN
    -- Try dropping the constraint by common naming patterns
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'payments_provider_payment_id_key') THEN
        ALTER TABLE payments DROP CONSTRAINT payments_provider_payment_id_key;
    END IF;
EXCEPTION
    WHEN OTHERS THEN NULL;
END $$;

-- Drop any unique index
DROP INDEX IF EXISTS ix_payments_provider_payment_id;

-- Create a regular (non-unique) index for lookups
CREATE INDEX IF NOT EXISTS ix_payments_provider_payment_id ON payments (provider_payment_id);
