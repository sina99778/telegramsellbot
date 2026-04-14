-- Migration 003: Add server domain fields, ticket photo support, and discount codes
-- Run this on your production database before deploying.

-- Server domain fields for config and subscription link generation
ALTER TABLE xui_servers ADD COLUMN IF NOT EXISTS config_domain VARCHAR(255);
ALTER TABLE xui_servers ADD COLUMN IF NOT EXISTS sub_domain VARCHAR(255);

-- Photo support in ticket messages
ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS photo_id VARCHAR(255);

-- Make ticket message text nullable (for photo-only messages)
ALTER TABLE ticket_messages ALTER COLUMN text DROP NOT NULL;

-- Discount codes table
CREATE TABLE IF NOT EXISTS discount_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) UNIQUE NOT NULL,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    plan_id UUID REFERENCES plans(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
