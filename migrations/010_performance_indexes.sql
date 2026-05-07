-- 010: Add performance indexes for common query patterns
-- Safe to run multiple times (IF NOT EXISTS)

-- Composite index on subscriptions(user_id, status) — used by dashboard, configs, sync
CREATE INDEX IF NOT EXISTS ix_subscriptions_user_status ON subscriptions (user_id, status);

-- Index on wallet_transactions(wallet_id) — used by transaction history
CREATE INDEX IF NOT EXISTS ix_wallet_transactions_wallet_id ON wallet_transactions (wallet_id);

-- Index on wallet_transactions(created_at) — used by ordering
CREATE INDEX IF NOT EXISTS ix_wallet_transactions_created_at ON wallet_transactions (created_at DESC);
