-- Migration: enrich audit_logs with request / state-diff context.
--
-- Without IP and before/after JSON it is very hard to investigate after the
-- fact ("who deleted this plan from where, and what did it look like?").

ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS ip_address INET NULL,
    ADD COLUMN IF NOT EXISTS user_agent TEXT NULL,
    ADD COLUMN IF NOT EXISTS request_path VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS before_state JSONB NULL,
    ADD COLUMN IF NOT EXISTS after_state JSONB NULL;

CREATE INDEX IF NOT EXISTS ix_audit_logs_ip_address_created_at
    ON audit_logs (ip_address, created_at);

CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created_at
    ON audit_logs (action, created_at);
