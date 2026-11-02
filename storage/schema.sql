-- Clean PostgreSQL baseline. Every business identity is account-scoped.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_platform_username
    ON accounts(platform, username) WHERE username <> '';

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform_conversation_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, platform_conversation_id),
    UNIQUE (account_id, id)
);

CREATE TABLE IF NOT EXISTS account_customers (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL,
    platform_user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    metadata TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, customer_id),
    UNIQUE (account_id, id)
);

CREATE TABLE IF NOT EXISTS tasks (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    capability TEXT NOT NULL,
    platform TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5 CHECK (priority >= 0),
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    error TEXT,
    result TEXT,
    send_status TEXT NOT NULL DEFAULT 'UNCERTAIN'
        CHECK (send_status IN ('SENT', 'NOT_SENT', 'UNCERTAIN')),
    send_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (send_attempt_count >= 0),
    message_signature TEXT,
    last_verified_at TIMESTAMPTZ,
    verification_source TEXT,
    owner_id TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, id),
    CHECK (status <> 'running' OR owner_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS actions (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id TEXT,
    action_name TEXT NOT NULL,
    args TEXT,
    success INTEGER CHECK (success IN (0, 1)),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id, task_id)
        REFERENCES tasks(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    account_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, task_id, checkpoint_name),
    FOREIGN KEY (account_id, task_id)
        REFERENCES tasks(account_id, id) ON DELETE CASCADE
);

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

CREATE TABLE IF NOT EXISTS customer_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, conversation_id),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL DEFAULT 'dom',
    message_hash TEXT,
    UNIQUE (account_id, conversation_id, message_id, direction),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS customer_profile (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    city TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, conversation_id),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS customer_facts (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, conversation_id, fact_type),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_events (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('RECEIVED', 'SENT', 'DELIVERY_UPDATED')),
    direction TEXT NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND', 'UNKNOWN')),
    status TEXT NOT NULL CHECK (status IN (
        'DETECTED', 'NEEDS_REPLY', 'APPROVAL_PENDING', 'APPROVED',
        'PROCESSING', 'REPLIED', 'IGNORED', 'FAILED')),
    error TEXT,
    reply_task_id TEXT,
    owner_id TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    detected_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_json TEXT NOT NULL,
    PRIMARY KEY (account_id, event_id),
    UNIQUE (account_id, dedup_key),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id, reply_task_id)
        REFERENCES tasks(account_id, id)
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    customer_name TEXT,
    text TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'QUEUED', 'SENT', 'FAILED')),
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, id),
    FOREIGN KEY (account_id, source_event_id)
        REFERENCES message_events(account_id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_escalations (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_json TEXT NOT NULL,
    analysis_json TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, event_id, reason),
    FOREIGN KEY (account_id, event_id)
        REFERENCES message_events(account_id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS send_tasks (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    task_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    inbound_message_id TEXT NOT NULL DEFAULT '',
    inbound_watermark TEXT NOT NULL DEFAULT '',
    reply_hash TEXT NOT NULL DEFAULT '',
    text_fingerprint TEXT NOT NULL DEFAULT '',
    attachment_hashes_json TEXT NOT NULL DEFAULT '[]',
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IN_FLIGHT'
        CHECK (state IN ('IN_FLIGHT', 'SENT', 'FAILED', 'ABORTED', 'EXPIRED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    error TEXT,
    failure_reason TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, signature),
    FOREIGN KEY (account_id, task_id)
        REFERENCES tasks(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inbox_ledger (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_key TEXT NOT NULL,
    conversation_name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('SETTLED', 'UNANSWERED')),
    status TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    inbound_message_id TEXT NOT NULL DEFAULT '',
    inbound_fingerprint TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, conversation_key)
);

CREATE TABLE IF NOT EXISTS message_request_snapshots (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'ACCEPTED', 'PROCESSED', 'FAILED', 'WAITING_HUMAN')),
    accepted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, request_key)
);

-- The supervisor can emit a notification before an account process registers,
-- so this table is account-scoped but intentionally has no account FK.
CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warn', 'bad', 'warning', 'error')),
    title TEXT NOT NULL,
    detail TEXT,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    dedup_key TEXT NOT NULL,
    UNIQUE (account_id, kind, dedup_key)
);

-- Partial indexes keep terminal history out of worker scans.
CREATE INDEX IF NOT EXISTS idx_tasks_claim_ready
    ON tasks(account_id, priority DESC, created_at, lease_expires_at)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_tasks_capability_active
    ON tasks(account_id, capability, lease_expires_at)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_tasks_message_signature
    ON tasks(account_id, message_signature) WHERE message_signature IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_actions_account_task ON actions(account_id, task_id);
CREATE INDEX IF NOT EXISTS idx_actions_created_brin ON actions USING BRIN(created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_recent
    ON conversation_messages(account_id, conversation_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_hash
    ON conversation_messages(account_id, conversation_id, message_hash)
    WHERE message_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_messages_created_brin
    ON conversation_messages USING BRIN(created_at);
CREATE INDEX IF NOT EXISTS idx_customer_facts_conversation
    ON customer_facts(account_id, conversation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_events_claim_ready
    ON message_events(account_id, detected_at, lease_expires_at)
    WHERE status IN ('DETECTED', 'PROCESSING');
CREATE INDEX IF NOT EXISTS idx_outbound_messages_pending
    ON outbound_messages(account_id, created_at)
    WHERE status IN ('PENDING', 'QUEUED');
CREATE INDEX IF NOT EXISTS idx_outbound_messages_conversation
    ON outbound_messages(account_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_send_tasks_active_lease
    ON send_tasks(account_id, lease_expires_at) WHERE state = 'IN_FLIGHT';
CREATE INDEX IF NOT EXISTS idx_send_tasks_conversation
    ON send_tasks(account_id, conversation_id, inbound_message_id);
CREATE INDEX IF NOT EXISTS idx_message_request_snapshots_status
    ON message_request_snapshots(account_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_open
    ON notifications(account_id, created_at DESC) WHERE status = 'OPEN';

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'accounts', 'conversations', 'account_customers', 'tasks',
        'task_checkpoints', 'customer_states', 'customer_profile', 'customer_facts',
        'message_events', 'outbound_messages', 'message_escalations', 'send_tasks',
        'inbox_ledger', 'message_request_snapshots'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_%I_updated_at BEFORE UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION set_updated_at()', table_name, table_name
        );
    END LOOP;
END;
$$;
