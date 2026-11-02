"""Conversation state and history for one account.

Bound to an account like every other repository: `account_id` is supplied by
the database it was opened for and injected into each statement, so a caller
cannot read or write another account's conversation by passing the wrong
conversation id.
"""

import logging

from models.account import require_account_id
from models.conversation import ConversationMessage
from models.customer_state import CustomerState
from models.message_event import MessageDirection
from storage.conversation_context import ConversationContextRepository


logger = logging.getLogger(__name__)


class PostgresConversationStore:
    def __init__(self, database, *, account_id=None, shop_name=None):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database
        # shop_name: the workspace's name for the Google Sheet's "Shop" column.
        self.context_repository = ConversationContextRepository(
            database, account_id=self.account_id, shop_name=shop_name,
        )

    async def get_state(self, conversation_id: str) -> CustomerState | None:
        row = self.database.conn.execute(
            "SELECT state_json FROM customer_states "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        ).fetchone()
        return CustomerState.model_validate_json(row["state_json"]) if row else None

    async def save_state(self, state: CustomerState):
        self.context_repository.register_conversation(
            state.conversation_id, state.customer_id
        )
        self.context_repository.register_customer(state.customer_id)
        self.database.conn.execute(
            "INSERT INTO customer_states("
            "account_id, conversation_id, customer_id, state_json, updated_at) "
            "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) "
            "ON CONFLICT(account_id, conversation_id) DO UPDATE SET "
            "customer_id=excluded.customer_id, "
            "state_json=excluded.state_json, updated_at=CURRENT_TIMESTAMP",
            (
                self.account_id, state.conversation_id, state.customer_id,
                state.model_dump_json(),
            ),
        )
        self.database.conn.commit()
        self.context_repository.export_to_sheet(state.conversation_id)

    async def append(self, message: ConversationMessage):
        self.context_repository.register_conversation(
            message.conversation_id, message.customer_id
        )
        self.context_repository.register_customer(message.customer_id)
        self.database.conn.execute(
            "INSERT INTO conversation_messages("
            "account_id, message_id, conversation_id, customer_id, direction, text, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                self.account_id, message.message_id, message.conversation_id,
                message.customer_id, message.direction.value, message.text,
                message.timestamp.isoformat(),
            ),
        )
        self.database.conn.commit()

    async def recent(self, conversation_id: str, limit=12) -> list[ConversationMessage]:
        rows = self.database.conn.execute(
            "SELECT * FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (self.account_id, conversation_id, max(1, int(limit))),
        ).fetchall()
        return [self._message(row) for row in reversed(rows)]

    async def history(self, conversation_id: str, limit=100) -> list[ConversationMessage]:
        return await self.recent(conversation_id, limit)

    async def last_outbound_text(self, conversation_id: str) -> str | None:
        pending = self.database.conn.execute(
            "SELECT text FROM outbound_messages "
            "WHERE account_id = %s AND conversation_id = %s "
            "AND status IN ('PENDING', 'QUEUED', 'SENT') ORDER BY created_at DESC LIMIT 1",
            (self.account_id, conversation_id),
        ).fetchone()
        if pending:
            return pending["text"]
        row = self.database.conn.execute(
            "SELECT text FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s "
            "AND direction = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (self.account_id, conversation_id, MessageDirection.OUTBOUND.value),
        ).fetchone()
        return row["text"] if row else None

    async def context(self, conversation_id: str, recent_limit=12):
        return await self.context_repository.get_context(
            conversation_id, recent_limit
        )

    async def merge_customer_facts(
        self, conversation_id, facts, source_message_id, confidence,
        *, username=None, display_name=None, customer_id=None, platform="",
        platform_user_id=None, customer_metadata=None,
    ):
        return await self.context_repository.merge_facts(
            conversation_id, facts, source_message_id, confidence,
            username=username, display_name=display_name,
            customer_id=customer_id, platform=platform,
            platform_user_id=platform_user_id,
            customer_metadata=customer_metadata,
        )

    @staticmethod
    def _message(row) -> ConversationMessage:
        return ConversationMessage(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            customer_id=row["customer_id"],
            direction=MessageDirection(row["direction"]),
            text=row["text"],
            timestamp=row["created_at"],
        )
