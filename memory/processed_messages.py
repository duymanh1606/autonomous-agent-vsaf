"""Idempotency store for inbound message events, scoped to one account.

The dedup key is derived from platform + conversation + message id, none of
which TikTok guarantees to be unique across accounts. Claiming on the key alone
would make account B's first sight of a message look "already processed"
because account A saw the same id - the customer would simply never get an
answer. Every statement here is therefore scoped by `account_id`, matching the
`UNIQUE(account_id, dedup_key)` constraint in the schema.
"""

import os
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from models.account import assert_account_scope, require_account_id
from models.message_event import MessageEvent, MessageEventStatus


class PostgresProcessedMessageStore:
    """Durable idempotency store; claim is a single unique insert."""

    def __init__(self, database, *, account_id=None, owner_id=None, lease_seconds=300):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database
        self.owner_id = str(owner_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        ))
        self.lease_seconds = max(30, int(lease_seconds))

    def _check(self, event, boundary):
        event_account = getattr(event, "account_id", None)
        if event_account:
            assert_account_scope(self.account_id, event_account, boundary=boundary)

    async def exists(self, event: MessageEvent) -> bool:
        self._check(event, "ProcessedMessages.exists")
        row = self.database.conn.execute(
            "SELECT 1 FROM message_events WHERE account_id = %s AND dedup_key = %s",
            (self.account_id, event.dedup_key),
        ).fetchone()
        return row is not None

    def _register_relationship(self, event):
        self.database.conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id, platform, customer_id) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(account_id, platform_conversation_id) DO UPDATE SET "
            "customer_id=COALESCE(NULLIF(excluded.customer_id, ''), conversations.customer_id)",
            (self.account_id, event.conversation_id, event.platform, event.customer_id),
        )
        self.database.conn.execute(
            "INSERT INTO account_customers(account_id, customer_id, username) "
            "VALUES (%s, %s, %s) ON CONFLICT(account_id, customer_id) DO UPDATE SET "
            "username=COALESCE(NULLIF(excluded.username, ''), account_customers.username)",
            (self.account_id, event.customer_id, event.customer_name or ""),
        )

    def _insert(self, event: MessageEvent, status: MessageEventStatus, *, claimed=False):
        now = datetime.now(timezone.utc)
        owner_id = self.owner_id if claimed else None
        claimed_at = now.isoformat() if claimed else None
        lease_expires_at = (
            (now + timedelta(seconds=self.lease_seconds)).isoformat() if claimed else None
        )
        return self.database.conn.execute(
            "INSERT INTO message_events("
            "event_id, account_id, dedup_key, platform, conversation_id, customer_id, "
            "message_id, event_type, direction, status, owner_id, claimed_at, "
            "lease_expires_at, attempt_count, detected_at, event_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                event.id, self.account_id, event.dedup_key, event.platform,
                event.conversation_id, event.customer_id, event.message_id,
                event.event_type.value, event.direction.value, status.value,
                owner_id, claimed_at, lease_expires_at,
                event.detected_at.isoformat(), event.model_dump_json(),
            ),
        )

    async def claim(self, event: MessageEvent) -> bool:
        self._check(event, "ProcessedMessages.claim")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self.lease_seconds)
        with self.database.conn.transaction():
            self._register_relationship(event)
            cursor = self._insert(event, MessageEventStatus.PROCESSING, claimed=True)
            if cursor.rowcount == 0:
                cursor = self.database.conn.execute(
                    "UPDATE message_events SET status = %s, owner_id = %s, claimed_at = %s, "
                    "lease_expires_at = %s, attempt_count = attempt_count + 1, error = NULL "
                    "WHERE account_id = %s AND dedup_key = %s AND (status = %s OR "
                    "(status = %s AND lease_expires_at <= %s))",
                    (
                        MessageEventStatus.PROCESSING.value, self.owner_id, now.isoformat(),
                        expires.isoformat(), self.account_id, event.dedup_key,
                        MessageEventStatus.DETECTED.value,
                        MessageEventStatus.PROCESSING.value, now.isoformat(),
                    ),
                )
        return cursor.rowcount == 1

    async def publish(self, event: MessageEvent) -> bool:
        self._check(event, "ProcessedMessages.publish")
        with self.database.conn.transaction():
            self._register_relationship(event)
            cursor = self._insert(event, MessageEventStatus.DETECTED)
        return cursor.rowcount == 1

    async def pending(self) -> list[MessageEvent]:
        now = datetime.now(timezone.utc).isoformat()
        self.database.conn.execute(
            "UPDATE message_events SET status = %s, owner_id = NULL, claimed_at = NULL, "
            "lease_expires_at = NULL WHERE account_id = %s AND status = %s "
            "AND lease_expires_at <= %s",
            (
                MessageEventStatus.DETECTED.value, self.account_id,
                MessageEventStatus.PROCESSING.value, now,
            ),
        )
        self.database.conn.commit()
        rows = self.database.conn.execute(
            "SELECT event_json FROM message_events "
            "WHERE account_id = %s AND status = %s ORDER BY detected_at ASC",
            (self.account_id, MessageEventStatus.DETECTED.value),
        ).fetchall()
        return [MessageEvent.model_validate_json(row["event_json"]) for row in rows]

    async def renew(self, event_id: str) -> bool:
        now = datetime.now(timezone.utc)
        cursor = self.database.conn.execute(
            "UPDATE message_events SET lease_expires_at = %s WHERE account_id = %s "
            "AND event_id = %s AND status = %s AND owner_id = %s",
            (
                (now + timedelta(seconds=self.lease_seconds)).isoformat(), self.account_id,
                event_id, MessageEventStatus.PROCESSING.value, self.owner_id,
            ),
        )
        self.database.conn.commit()
        return cursor.rowcount == 1

    async def mark(self, event_id: str, status: MessageEventStatus, error: str | None = None):
        self.database.conn.execute(
            "UPDATE message_events SET status = %s, error = %s, "
            "processed_at = CURRENT_TIMESTAMP, owner_id = NULL, lease_expires_at = NULL "
            "WHERE account_id = %s AND event_id = %s AND owner_id = %s",
            (status.value, error, self.account_id, event_id, self.owner_id),
        )
        self.database.conn.commit()
