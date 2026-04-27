CREATE TABLE IF NOT EXISTS plan_inventories (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    plan_id UUID NOT NULL UNIQUE REFERENCES plans(id) ON DELETE CASCADE,
    sales_limit INTEGER NOT NULL DEFAULT 0,
    sold_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_plan_inventories_plan_id ON plan_inventories(plan_id);
