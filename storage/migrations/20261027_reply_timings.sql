-- How long each step of answering one customer took (agent/reply_timing.py).
-- One row per conversation the reply pipeline ran for, whatever the outcome;
-- `status` is the scan outcome (sent, requires_human, reconcile_required...).
-- `detail` holds the pipeline's own steps (llm_analyze, llm_generate...).
-- Row-level security is added on startup by storage/tenancy.py.
CREATE TABLE IF NOT EXISTS reply_timings (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_key TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    burst_size INTEGER NOT NULL DEFAULT 0,
    open_ms INTEGER,
    hold_ms INTEGER,
    reply_ms INTEGER,
    recheck_ms INTEGER,
    send_ms INTEGER,
    verify_ms INTEGER,
    total_ms INTEGER NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reply_timings_account_created
    ON reply_timings(account_id, created_at DESC);
