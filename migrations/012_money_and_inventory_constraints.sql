-- Migration: defence-in-depth CHECK constraints on money + inventory tables.
--
-- These mirror the app-level invariants so a stray UPDATE/INSERT or buggy
-- code path cannot leave the system in an impossible state.
--
-- IMPORTANT design notes that affect which constraints we DO and DO NOT add:
--
-- 1. Reseller wallets are allowed negative balances down to `-credit_limit`
--    (see services/wallet/manager.py:81). So the wallets.balance constraint
--    must respect credit_limit, NOT be a flat `>= 0`.
--
-- 2. `wallet_transactions` is an immutable ledger. Its `balance_after`
--    records whatever the wallet balance was at that point in time,
--    including legitimate negative-balance moments for resellers. We do
--    NOT constrain balance_after to be non-negative — that would break
--    legitimate ledger history.
--
-- 3. plan_inventories.sales_limit == 0 means "unlimited" by convention
--    (see services/plan_inventory.py UNLIMITED_STOCK_LIMIT). The
--    sold-count-exceeds-limit check guards against that sentinel.

DO $$
BEGIN
    -- Wallet live balance: respect credit_limit. A reseller with
    -- credit_limit=10 may legitimately have balance=-10.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_wallets_balance_within_credit_limit') THEN
        ALTER TABLE wallets ADD CONSTRAINT ck_wallets_balance_within_credit_limit
            CHECK (balance >= -credit_limit);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_wallets_credit_limit_non_negative') THEN
        ALTER TABLE wallets ADD CONSTRAINT ck_wallets_credit_limit_non_negative CHECK (credit_limit >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_wallets_hold_balance_non_negative') THEN
        ALTER TABLE wallets ADD CONSTRAINT ck_wallets_hold_balance_non_negative CHECK (hold_balance >= 0);
    END IF;

    -- Wallet transactions: amount is always positive; direction is a small
    -- enum. We do NOT enforce balance_after >= 0 (see note above).
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_wallet_tx_amount_positive') THEN
        ALTER TABLE wallet_transactions ADD CONSTRAINT ck_wallet_tx_amount_positive CHECK (amount > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_wallet_tx_direction') THEN
        ALTER TABLE wallet_transactions ADD CONSTRAINT ck_wallet_tx_direction CHECK (direction IN ('credit', 'debit'));
    END IF;

    -- Discount codes
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_discount_percent_range') THEN
        ALTER TABLE discount_codes ADD CONSTRAINT ck_discount_percent_range CHECK (discount_percent BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_discount_max_uses_positive') THEN
        ALTER TABLE discount_codes ADD CONSTRAINT ck_discount_max_uses_positive CHECK (max_uses >= 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_discount_used_count_non_negative') THEN
        ALTER TABLE discount_codes ADD CONSTRAINT ck_discount_used_count_non_negative CHECK (used_count >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_discount_used_not_exceed_max') THEN
        ALTER TABLE discount_codes ADD CONSTRAINT ck_discount_used_not_exceed_max CHECK (used_count <= max_uses);
    END IF;

    -- Plan inventory: 0 sales_limit means unlimited.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_inv_sold_non_negative') THEN
        ALTER TABLE plan_inventories ADD CONSTRAINT ck_plan_inv_sold_non_negative CHECK (sold_count >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_inv_limit_non_negative') THEN
        ALTER TABLE plan_inventories ADD CONSTRAINT ck_plan_inv_limit_non_negative CHECK (sales_limit >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_inv_sold_not_exceed_limit') THEN
        ALTER TABLE plan_inventories ADD CONSTRAINT ck_plan_inv_sold_not_exceed_limit
            CHECK (sales_limit <= 0 OR sold_count <= sales_limit);
    END IF;
END $$;
