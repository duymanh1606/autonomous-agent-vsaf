-- Outside alerting (dashboard_api/alerts.py). `created_at` is rewritten on
-- every re-record of an open notification, so it cannot say how long a fault
-- has lasted; `opened_at` only moves when the notification (re)opens.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS alerted_at TIMESTAMPTZ;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS recovery_alerted_at TIMESTAMPTZ;
