-- Additive upgrade retained for databases created during clean-schema rollout.
-- Fresh databases already contain these objects in the immutable baseline.

ALTER TABLE message_events ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE message_events ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE message_events ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE message_events ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE message_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_message_events_claim_ready
ON message_events(account_id, detected_at, lease_expires_at)
WHERE status IN ('DETECTED', 'PROCESSING');

CREATE TABLE IF NOT EXISTS screen_transition_stats (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    from_screen TEXT NOT NULL,
    action_name TEXT NOT NULL,
    to_screen TEXT NOT NULL,
    observation_count BIGINT NOT NULL DEFAULT 1 CHECK (observation_count > 0),
    average_confidence DOUBLE PRECISION NOT NULL
        CHECK (average_confidence BETWEEN 0 AND 1),
    max_confidence DOUBLE PRECISION NOT NULL CHECK (max_confidence BETWEEN 0 AND 1),
    last_confidence DOUBLE PRECISION NOT NULL CHECK (last_confidence BETWEEN 0 AND 1),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, from_screen, action_name, to_screen)
);
