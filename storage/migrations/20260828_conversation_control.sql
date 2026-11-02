CREATE TABLE IF NOT EXISTS conversation_control (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,

    mode TEXT NOT NULL DEFAULT 'AUTO'
        CHECK (mode IN ('AUTO', 'WAITING_HUMAN', 'HUMAN')),

    operator_id TEXT,
    takeover_source TEXT    
        CHECK (
            takeover_source IS NULL
            OR takeover_source IN ('MANUAL', 'ESCALATION')
        ),

    reason TEXT,

    taken_over_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,


    PRIMARY KEY (account_id, conversation_id),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, platform_conversation_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conversation_control_mode
    ON conversation_control(account_id, mode);