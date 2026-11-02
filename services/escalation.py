from typing import Protocol

from models.account import require_account_id
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent


class EscalationService(Protocol):
    async def escalate(
        self, event: MessageEvent, state: CustomerState, reason: str,
        analysis: MessageAnalysis | None = None,
    ): ...


class PostgresEscalationService:
    def __init__(self, database, *, account_id=None):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database

    async def escalate(
        self,
        event: MessageEvent,
        state: CustomerState,
        reason: str,
        analysis: MessageAnalysis | None = None,
    ):
        with self.database.conn.transaction():
            self.database.conn.execute(
                "INSERT INTO conversations(account_id, platform_conversation_id, platform, "
                "customer_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(account_id, platform_conversation_id) DO UPDATE SET "
                "customer_id=COALESCE(NULLIF(excluded.customer_id, ''), conversations.customer_id)",
                (self.account_id, event.conversation_id, event.platform, event.customer_id),
            )
            # 'IGNORED', never 'DETECTED': a 'DETECTED' row is an inbound
            # message still owed an answer (`PostgresProcessedMessageStore.
            # pending()`), and a since-removed queue-driven reply path once
            # answered exactly those on every restart - so an escalation row
            # inserted as 'DETECTED' got the customer a second, unreviewed
            # reply, the very thing this table exists to prevent. ON CONFLICT
            # DO NOTHING keeps an event that already has a real row as it is.
            inserted = self.database.conn.execute(
                "INSERT INTO message_events(event_id, account_id, dedup_key, platform, "
                "conversation_id, customer_id, message_id, event_type, direction, status, "
                "detected_at, event_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'IGNORED', %s, %s) ON CONFLICT(account_id, dedup_key) DO NOTHING "
                "RETURNING event_id",
                (
                    event.id, self.account_id, event.dedup_key, event.platform,
                    event.conversation_id, event.customer_id, event.message_id,
                    event.event_type.value, event.direction.value,
                    event.detected_at.isoformat(), event.model_dump_json(),
                ),
            ).fetchone()
            event_id = event.id
            if inserted is None:
                existing = self.database.conn.execute(
                    "SELECT event_id FROM message_events WHERE account_id = %s AND dedup_key = %s",
                    (self.account_id, event.dedup_key),
                ).fetchone()
                event_id = existing["event_id"]
            self.database.conn.execute(
                "INSERT INTO message_escalations("
                "account_id, event_id, conversation_id, customer_id, reason, state_json, "
                "analysis_json) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    self.account_id, event_id, event.conversation_id, event.customer_id,
                    reason, state.model_dump_json(),
                    analysis.model_dump_json() if analysis else None,
                ),
            )
