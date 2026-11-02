"""Durable dashboard notifications stored in shared PostgreSQL.

The dashboard owns these rows, but they use the same PostgreSQL service as the
agent so the project has one storage engine. The connection is lazy: importing
the supervisor remains safe while PostgreSQL is starting.
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from threading import RLock
from typing import Any

import psycopg
from psycopg.rows import dict_row

from storage.database import resolve_dsn


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id          BIGSERIAL PRIMARY KEY,
    account_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    title       TEXT NOT NULL,
    detail      TEXT,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    dedup_key   TEXT NOT NULL,
    UNIQUE(account_id, kind, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_notifications_open
    ON notifications(account_id, created_at DESC) WHERE status = 'OPEN';
-- Same columns as storage/migrations/20261016_notification_alerts.sql: the
-- supervisor can run before any agent has applied that migration.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS alerted_at TIMESTAMPTZ;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS recovery_alerted_at TIMESTAMPTZ;
"""

NOTIFIABLE = {
    "escalation.opened": ("warn", "Cần người xử lý"),
    "agent.stopping": ("info", "Agent đang dừng"),
    "auth.security_challenge": ("bad", "Cần giải CAPTCHA/xác minh bảo mật"),
    "auth.rate_limited": ("warn", "TikTok tạm giới hạn đăng nhập"),
    "auth.required": ("bad", "Mất phiên đăng nhập"),
    "workspace.degraded": ("bad", "Workspace không tự đánh thức được"),
}

# Escalation reasons an operator should read as a sentence, not a code: the
# title says what happened, the detail what to do.
ESCALATION_REASONS = {
    "reply_withheld:order_ready": (
        "Đơn hàng đủ thông tin, cần tạo đơn",
        "Khách đang đặt hàng và đã gửi số điện thoại + địa chỉ. Agent đã trả lời và "
        "dừng ở hội thoại này - xác nhận số lượng, tạo đơn trên trang Đơn hàng rồi "
        "trả lời khách.",
    ),
    "routed_to_human:order_status": (
        "Khách hỏi tình trạng đơn hàng",
        "Agent không tra được vận đơn nên không trả lời thay - kiểm tra đơn và "
        "báo lại cho khách.",
    ),
    "reply_withheld:unsupported_message_type": (
        "Khách gửi tin nhắn thoại / loại không xem được trên web",
        "TikTok Web không hiển thị loại tin này. Khách đã được báo chờ nhân viên - "
        "mở app TikTok trên điện thoại để nghe/xem rồi trả lời khách.",
    ),
}

# One open notification per account for these - a workspace stuck in the
# same blocked state keeps updating the same row (see NotificationStore.record's
# ON CONFLICT), never piling up duplicates.
AUTH_BLOCKED_KINDS = ("auth.security_challenge", "auth.rate_limited", "auth.required")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = str(dsn or resolve_dsn())
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._lock = RLock()

    def connect(self):
        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(
                    self.dsn, row_factory=dict_row, autocommit=True,
                    connect_timeout=5,
                )
                self._conn.execute(SCHEMA)
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    def record(self, *, account_id: str, kind: str, title: str,
               detail: str = "", severity: str = "info",
               dedup_key: str = "", payload: dict | None = None) -> None:
        with self._lock:
            self.connect().execute(
                """INSERT INTO notifications
                     (account_id, kind, severity, title, detail, payload,
                      status, created_at, opened_at, dedup_key)
                   VALUES (%s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
                   ON CONFLICT(account_id, kind, dedup_key) DO UPDATE SET
                     title=excluded.title,
                     detail=excluded.detail,
                     severity=excluded.severity,
                     payload=excluded.payload,
                     created_at=excluded.created_at,
                     -- The SET list sees the row as it was: a fault that is
                     -- still open keeps its start and its alert state, one
                     -- that had been resolved starts a new episode.
                     opened_at=CASE WHEN notifications.status = 'OPEN'
                               THEN notifications.opened_at ELSE excluded.opened_at END,
                     alerted_at=CASE WHEN notifications.status = 'OPEN'
                                THEN notifications.alerted_at ELSE NULL END,
                     recovery_alerted_at=NULL,
                     status='OPEN',
                     resolved_at=NULL""",
                (account_id, kind, severity, title, detail,
                 json.dumps(payload or {}, ensure_ascii=False), _now(), _now(),
                 dedup_key or kind),
            )

    def from_event(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type", ""))
        account_id = str(event.get("account_id", ""))
        payload = event.get("payload") or {}

        if event_type == "health.changed":
            component = str(payload.get("component", "unknown"))
            if payload.get("ok"):
                self.resolve_kind(account_id, "health", component)
                return False
            self.record(
                account_id=account_id, kind="health", severity="bad",
                title=f"{component} mất kết nối",
                detail=str(payload.get("detail", "")),
                dedup_key=component, payload=payload,
            )
            return True

        if event_type == "workspace.recovered":
            # A sleeping workspace woke up fine after previously exhausting
            # its wake retries - close the DEGRADED notification the same
            # way auth.recovered closes the blocked-auth ones below.
            self.resolve_kind(account_id, "workspace.degraded", "workspace.degraded")
            return False

        if event_type == "auth.recovered":
            # Logged back in (or the CAPTCHA/rate-limit cleared) - close
            # whichever of the three blocked-auth notifications was open for
            # this account, same pattern as health.changed's ok=True branch.
            for kind in AUTH_BLOCKED_KINDS:
                self.resolve_kind(account_id, kind, kind)
            return False

        entry = NOTIFIABLE.get(event_type)
        if entry is None:
            return False
        severity, title = entry
        detail = str(payload.get("reason") or payload.get("detail") or "")
        if event_type == "escalation.opened" and detail in ESCALATION_REASONS:
            title, detail = ESCALATION_REASONS[detail]
            conversation = payload.get("conversation")
            if conversation:
                title = f"{title}: {conversation}"
        self.record(
            account_id=account_id, kind=event_type, severity=severity,
            title=title,
            detail=detail,
            dedup_key=str(
                payload.get("conversation") or payload.get("session_id") or event_type
            ),
            payload=payload,
        )
        return True

    def get(self, notification_id: int) -> dict[str, Any] | None:
        """Look up one notification's owning account - used by the API route
        to check workspace access before letting a non-admin resolve it."""
        with self._lock:
            row = self.connect().execute(
                "SELECT * FROM notifications WHERE id=%s", (notification_id,),
            ).fetchone()
            return dict(row) if row else None

    def resolve(self, notification_id: int) -> bool:
        with self._lock:
            cursor = self.connect().execute(
                "UPDATE notifications SET status='RESOLVED', resolved_at=%s "
                "WHERE id=%s AND status='OPEN'",
                (_now(), notification_id),
            )
            return cursor.rowcount > 0

    def resolve_kind(self, account_id: str, kind: str, dedup_key: str) -> None:
        with self._lock:
            self.connect().execute(
                "UPDATE notifications SET status='RESOLVED', resolved_at=%s "
                "WHERE account_id=%s AND kind=%s AND dedup_key=%s AND status='OPEN'",
                (_now(), account_id, kind, dedup_key),
            )

    def list(self, *, status: str = "OPEN", account_id: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = ["status = %s"], [status]
        if account_id:
            clauses.append("account_id = %s")
            params.append(account_id)
        with self._lock:
            rows = self.connect().execute(
                f"""SELECT * FROM notifications WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC LIMIT %s""",
                (*params, int(limit)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except ValueError:
                item["payload"] = {}
            result.append(item)
        return result

    # ------------------------------------------------- outside alerting
    # dashboard_api/alerts.py. Global on purpose, like open_count(): the
    # mail goes to the installation's operators, not to one workspace.
    def pending_alerts(self, *, severities, grace_seconds: int) -> builtins.list[dict[str, Any]]:
        """Open faults of these severities, older than the grace, not yet mailed."""
        with self._lock:
            rows = self.connect().execute(
                """SELECT id, account_id, kind, severity, title, detail, opened_at
                     FROM notifications
                    WHERE status = 'OPEN' AND alerted_at IS NULL
                      AND severity = ANY(%s)
                      AND opened_at <= now() - make_interval(secs => %s)
                 ORDER BY opened_at""",
                (list(severities), int(grace_seconds)),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_recoveries(self) -> builtins.list[dict[str, Any]]:
        """Faults that were mailed and have since cleared.

        Not escalations: one is closed by a person handling the customer, and
        "someone handled it" is not news to mail to the people who did.
        """
        with self._lock:
            rows = self.connect().execute(
                """SELECT id, account_id, kind, severity, title, detail,
                          opened_at, resolved_at
                     FROM notifications
                    WHERE status = 'RESOLVED' AND alerted_at IS NOT NULL
                      AND recovery_alerted_at IS NULL
                      AND kind <> 'escalation.opened'
                 ORDER BY resolved_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_escalations(self, *, grace_seconds: int) -> builtins.list[dict[str, Any]]:
        """Open "a person must take over" notifications not yet mailed.

        Only the conversation's name comes out of the payload: the customer's
        own words stay in the database and do not travel by mail.
        """
        with self._lock:
            rows = self.connect().execute(
                """SELECT id, account_id, kind, title, detail, opened_at,
                          payload::jsonb ->> 'conversation' AS conversation
                     FROM notifications
                    WHERE status = 'OPEN' AND alerted_at IS NULL
                      AND kind = 'escalation.opened'
                      AND opened_at <= now() - make_interval(secs => %s)
                 ORDER BY opened_at""",
                (int(grace_seconds),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_alerted(self, ids) -> None:
        if ids:
            with self._lock:
                self.connect().execute(
                    "UPDATE notifications SET alerted_at = %s WHERE id = ANY(%s)",
                    (_now(), list(ids)),
                )

    def mark_recovery_alerted(self, ids) -> None:
        if ids:
            with self._lock:
                self.connect().execute(
                    "UPDATE notifications SET recovery_alerted_at = %s WHERE id = ANY(%s)",
                    (_now(), list(ids)),
                )

    def open_count(self) -> int:
        with self._lock:
            row = self.connect().execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE status='OPEN'"
            ).fetchone()
        return int(row["n"]) if row else 0
