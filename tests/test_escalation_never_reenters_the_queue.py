"""Regression: an escalation row must never read as a fresh, unanswered
inbound message.

`PostgresEscalationService.escalate()` used to insert its `message_events` row
with `status='DETECTED'`. A queue-driven reply path (since removed) swept every
`DETECTED` row back into its live queue on each restart - and the
conversation-control TTL auto-release opened the same window within minutes -
so a purely informational escalation record was answered like a brand-new
customer message, without anyone reviewing the case it was raised for. That
path is gone, but `PostgresProcessedMessageStore.pending()` still means
"unanswered work", so an escalation row must stay out of it. See
services/escalation.py for the fix.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory.processed_messages import PostgresProcessedMessageStore
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from services.escalation import PostgresEscalationService
from tests.support import make_database


def handoff_event(conversation_id="helloworld!!", message_id="dom-9374e554"):
    """Shaped exactly like `services.handoff.HumanHandoff._event()` builds
    it: a fresh id/dedup_key the queue-driven ingestion pipeline has never
    seen, because it comes from the independent DOM-scan path."""
    return MessageEvent(
        id=f"handoff-{conversation_id}-{message_id}",
        account_id="acc_test",
        platform="tiktok_web",
        conversation_id=conversation_id,
        customer_id=conversation_id,
        message_id=message_id,
        message="Gửi giúp mình hình ảnh sản phẩm",
        timestamp=datetime.now(timezone.utc),
    )


class EscalationRowNeverReenqueuedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fresh_escalation_row_is_never_returned_by_pending(self):
        database = make_database(self)
        escalation = PostgresEscalationService(database)
        processed = PostgresProcessedMessageStore(database)

        event = handoff_event()
        await escalation.escalate(
            event,
            CustomerState(
                conversation_id=event.conversation_id,
                customer_id=event.conversation_id,
                stage="human_required", requires_human=True,
            ),
            "delivery_uncertain_requires_reconciliation",
        )

        # `pending()` is "inbound messages still owed an answer" - what the
        # removed queue path re-queued on every start. An escalation row in
        # it would be a standing invitation for any future consumer to send
        # a second, unreviewed reply to the customer the escalation protects.
        pending = await processed.pending()
        self.assertEqual(pending, [])

    async def test_escalating_the_same_event_twice_stays_one_row(self):
        database = make_database(self)
        escalation = PostgresEscalationService(database)
        state = CustomerState(
            conversation_id="helloworld!!", customer_id="helloworld!!",
            stage="human_required", requires_human=True,
        )
        event = handoff_event()

        await escalation.escalate(event, state, "reason_one")
        await escalation.escalate(event, state, "reason_one")

        rows = database.conn.execute(
            "SELECT count(*) AS n FROM message_events WHERE account_id = %s "
            "AND event_id = %s",
            (database.account_id, event.id),
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    async def test_escalating_an_event_already_tracked_by_the_queue_never_downgrades_it(self):
        """The `processing_failed:*` path in agent/processor.py calls
        `escalate()` for an event that already has a real row in this table
        (inserted earlier by the normal queue-driven `publish()`). That row's
        actual status - whatever the queue lifecycle set it to - must survive
        untouched; `escalate()`'s own insert is a no-op via
        `ON CONFLICT DO NOTHING`."""
        database = make_database(self)
        escalation = PostgresEscalationService(database)
        processed = PostgresProcessedMessageStore(database)
        state = CustomerState(
            conversation_id="known-customer", customer_id="known-customer",
            stage="human_required", requires_human=True,
        )
        event = MessageEvent(
            id="evt-1", account_id="acc_test", platform="tiktok_web",
            conversation_id="known-customer", customer_id="known-customer",
            message_id="m1", message="Chào shop",
            timestamp=datetime.now(timezone.utc),
        )

        await processed.publish(event)
        # The event is still legitimately pending at this point.
        self.assertEqual(len(await processed.pending()), 1)

        await escalation.escalate(event, state, "processing_failed:SomeError")

        # escalate() must not have flipped this genuinely-pending event to
        # IGNORED behind the queue's back - only `processed.mark(...)` (the
        # queue's own lifecycle, called right after escalate() in
        # agent/processor.py) decides that.
        self.assertEqual(len(await processed.pending()), 1)


if __name__ == "__main__":
    unittest.main()
