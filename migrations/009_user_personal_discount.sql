-- Migration 009: Add personal_discount_percent to users table
-- Allows admins to set per-user custom discount percentages

ALTER TABLE users ADD COLUMN IF NOT EXISTS personal_discount_percent INTEGER NOT NULL DEFAULT 0;
