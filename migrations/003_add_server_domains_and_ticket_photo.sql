-- Migration 003: Add server domain fields and ticket message photo support
-- Run this on your production database before deploying.

-- Server domain fields for config and subscription link generation
ALTER TABLE xui_servers ADD COLUMN IF NOT EXISTS config_domain VARCHAR(255);
ALTER TABLE xui_servers ADD COLUMN IF NOT EXISTS sub_domain VARCHAR(255);

-- Photo support in ticket messages
ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS photo_id VARCHAR(255);

-- Make ticket message text nullable (for photo-only messages)
ALTER TABLE ticket_messages ALTER COLUMN text DROP NOT NULL;
