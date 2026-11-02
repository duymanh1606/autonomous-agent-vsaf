-- Idempotent, additive migration for task/send leases and Message requests.
-- No row is deleted and every new required value has a safe default.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_tasks_claim_lease
ON tasks(account_id, capability, status, lease_expires_at);

ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS inbound_watermark TEXT NOT NULL DEFAULT '';
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS text_fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS attachment_hashes_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT '';
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_send_tasks_lease
ON send_tasks(account_id, state, lease_expires_at);

CREATE TABLE IF NOT EXISTS message_request_snapshots (
    account_id       TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    request_key      TEXT NOT NULL,
    conversation_id  TEXT NOT NULL DEFAULT '',
    username         TEXT NOT NULL DEFAULT '',
    snapshot_json    TEXT NOT NULL,
    classification   TEXT NOT NULL DEFAULT 'unknown',
    status           TEXT NOT NULL DEFAULT 'PENDING',
    accepted_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, request_key)
);

CREATE INDEX IF NOT EXISTS idx_message_request_snapshots_status
ON message_request_snapshots(account_id, status, updated_at DESC);
