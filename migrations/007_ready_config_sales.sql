CREATE TABLE IF NOT EXISTS ready_config_pools (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    plan_id UUID NOT NULL UNIQUE REFERENCES plans(id) ON DELETE CASCADE,
    is_active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ready_config_items (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    pool_id UUID NOT NULL REFERENCES ready_config_pools(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'available',
    assigned_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    order_id UUID NULL REFERENCES orders(id) ON DELETE SET NULL,
    subscription_id UUID NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
    source_name VARCHAR(255) NULL,
    line_number INTEGER NULL,
    sold_at TIMESTAMP WITH TIME ZONE NULL
);

CREATE INDEX IF NOT EXISTS ix_ready_config_items_pool_status_created
    ON ready_config_items(pool_id, status, created_at);

CREATE INDEX IF NOT EXISTS ix_ready_config_items_subscription_id
    ON ready_config_items(subscription_id);
