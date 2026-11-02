"""Two accounts, one shared PostgreSQL database, no leakage.

These tests are the reason the account scope exists. They deliberately use the
*worst case*: both accounts talk to the same customer, in a conversation with
the same platform id, about the same message id, with identical text. Anything
that keys on the customer, the conversation or the message alone will merge the
two - and that merge is a real customer receiving another shop's answer.

Everything below runs against one shared database, exactly as production does.
"""

import unittest
from dataclasses import FrozenInstanceError

from pydantic import ValidationError

from memory.conversation_store import PostgresConversationStore
from memory.processed_messages import PostgresProcessedMessageStore
from models.account import (
    AccountContext,
    AccountIsolationError,
    assert_account_scope,
    require_account_id,
)
from models.message_event import (
    MessageDirection,
    MessageEvent,
)
from models.tasks import Task
from storage.audit import audit_project
from storage.database import Database
from storage.scope import RepositoryScope
from tests.support import SCHEMA_PATH, TemporaryPostgresSchema, make_database, unscoped_connection


SHARED_CONVERSATION = "conv-shared-1"
SHARED_CUSTOMER = "@abc"


class VisibleMessage:
    """The shape `store_visible_messages` reads."""

    def __init__(self, direction, text):
        self.direction = direction
        self.text = text


def event(account_id, text="xin chao", message_id="m-1"):
    return MessageEvent(
        account_id=account_id,
        platform="tiktok_web",
        conversation_id=SHARED_CONVERSATION,
        customer_id=SHARED_CUSTOMER,
        message_id=message_id,
        message=text,
        timestamp="2026-01-01T00:00:00+00:00",
        direction=MessageDirection.INBOUND,
    )


class AccountIdentityTests(unittest.TestCase):
    """`account_id` is never invented, never defaulted."""

    def test_an_absent_account_id_is_refused_rather_than_defaulted(self):
        for value in ("", "   ", None):
            with self.assertRaises(AccountIsolationError):
                require_account_id(value)

    def test_a_mismatch_names_both_sides_and_the_boundary(self):
        with self.assertRaises(AccountIsolationError) as caught:
            assert_account_scope("acc_001", "acc_002", boundary="Task -> Runtime")
        message = str(caught.exception)
        self.assertIn("acc_001", message)
        self.assertIn("acc_002", message)
        self.assertIn("Task -> Runtime", message)

    def test_matching_accounts_pass_through(self):
        self.assertEqual(assert_account_scope("acc_001", "acc_001"), "acc_001")

    def test_a_context_cannot_be_retargeted_at_another_account(self):
        context = AccountContext(account_id="acc_001", platform="tiktok_web")
        with self.assertRaises(FrozenInstanceError):
            context.account_id = "acc_002"

    def test_a_task_without_an_account_cannot_be_built(self):
        with self.assertRaises(ValidationError):
            Task(id="t1", capability="open_inbox", platform="tiktok_web")


class SharedDatabaseIsolationTests(unittest.IsolatedAsyncioTestCase):
    """One database, two accounts, the same customer on both."""

    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.db_a = Database(self.schema.dsn, SCHEMA_PATH, account_id="acc_001")
        self.addCleanup(self.db_a.close)
        self.db_b = Database(self.schema.dsn, SCHEMA_PATH, account_id="acc_002")
        self.addCleanup(self.db_b.close)
        self.a = RepositoryScope(self.db_a, "acc_001")
        self.b = RepositoryScope(self.db_b, "acc_002")

    async def test_the_same_conversation_id_is_two_conversations(self):
        await self.a.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", "ben A hoi gia")],
            customer_id=SHARED_CUSTOMER,
        )
        await self.b.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", "ben B hoi ton kho")],
            customer_id=SHARED_CUSTOMER,
        )
        self.assertEqual(
            [m["text"] for m in await self.a.customers.load_recent_messages(SHARED_CONVERSATION)],
            ["ben A hoi gia"],
        )
        self.assertEqual(
            [m["text"] for m in await self.b.customers.load_recent_messages(SHARED_CONVERSATION)],
            ["ben B hoi ton kho"],
        )

    async def test_identical_text_from_one_customer_is_not_deduplicated_across_accounts(self):
        """Both dedup guards are account-scoped, so neither row is swallowed.

        `UNIQUE(account_id, conversation_id, message_hash)` and
        `UNIQUE(account_id, conversation_id, message_id, direction)` each carry
        the account. Drop `account_id` from either and account B's message is
        silently discarded as a duplicate of account A's.
        """
        identical = "shop oi con hang khong"
        stored_a = await self.a.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", identical)],
            customer_id=SHARED_CUSTOMER,
        )
        stored_b = await self.b.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", identical)],
            customer_id=SHARED_CUSTOMER,
        )
        self.assertEqual(len(stored_a), 1)
        self.assertEqual(len(stored_b), 1, "account B's message was swallowed as a duplicate")

        # Unscoped: under row-level security db_a would only ever see acc_001.
        rows = unscoped_connection(self, self.db_a).execute(
            "SELECT account_id FROM conversation_messages WHERE text = %s ORDER BY account_id",
            (identical,),
        ).fetchall()
        self.assertEqual([r["account_id"] for r in rows], ["acc_001", "acc_002"])

    async def test_customer_facts_never_cross_accounts(self):
        await self.a.customers.merge_facts(
            SHARED_CONVERSATION, {"phone": "0900000001"}, "m-1", 0.95
        )
        await self.b.customers.merge_facts(
            SHARED_CONVERSATION, {"phone": "0900000002"}, "m-1", 0.95
        )
        facts_a = await self.a.customers.get_customer_facts(SHARED_CONVERSATION)
        facts_b = await self.b.customers.get_customer_facts(SHARED_CONVERSATION)
        self.assertEqual(facts_a["phone"]["value"], "0900000001")
        self.assertEqual(facts_b["phone"]["value"], "0900000002")

    async def test_the_llm_context_of_one_account_holds_only_its_own_thread(self):
        await self.a.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", "chi A")],
            customer_id=SHARED_CUSTOMER,
        )
        await self.b.customers.store_visible_messages(
            SHARED_CONVERSATION, [VisibleMessage("INBOUND", "chi B")],
            customer_id=SHARED_CUSTOMER,
        )
        await self.a.customers.merge_facts(
            SHARED_CONVERSATION, {"city": "Ha Noi"}, "m-1", 0.95
        )
        context = await self.a.customers.get_context(SHARED_CONVERSATION)
        self.assertEqual(context["account_id"], "acc_001")
        self.assertEqual([m["text"] for m in context["recent_messages"]], ["chi A"])
        self.assertEqual(context["customer_facts"]["city"]["value"], "Ha Noi")

        context_b = await self.b.customers.get_context(SHARED_CONVERSATION)
        self.assertEqual([m["text"] for m in context_b["recent_messages"]], ["chi B"])
        self.assertNotIn("city", context_b["customer_facts"])

    async def test_a_task_queue_only_yields_its_own_accounts_work(self):
        await self.a.tasks.submit(Task(
            id="task-a", account_id="acc_001", capability="messaging.scan_inbox",
            platform="tiktok_web",
        ))
        await self.b.tasks.submit(Task(
            id="task-b", account_id="acc_002", capability="messaging.scan_inbox",
            platform="tiktok_web",
        ))
        self.assertEqual((await self.a.tasks.get_next_task()).id, "task-a")
        self.assertEqual((await self.b.tasks.get_next_task()).id, "task-b")
        self.assertIsNone(await self.a.tasks.get_next_task())

    async def test_an_active_capability_of_another_account_does_not_block_this_one(self):
        await self.a.tasks.submit(Task(
            id="task-a", account_id="acc_001", capability="messaging.scan_inbox",
            platform="tiktok_web",
        ))
        self.assertTrue(await self.a.tasks.has_active_capability("messaging.scan_inbox"))
        self.assertFalse(
            await self.b.tasks.has_active_capability("messaging.scan_inbox"),
            "account B was blocked by account A's running scan",
        )

    async def test_the_same_platform_message_id_is_claimable_once_per_account(self):
        store_a = PostgresProcessedMessageStore(self.db_a)
        store_b = PostgresProcessedMessageStore(self.db_b)
        self.assertTrue(await store_a.claim(event("acc_001")))
        self.assertTrue(
            await store_b.claim(event("acc_002")),
            "account B could not claim a message because account A had seen the same id",
        )
        # ...but only once inside one account.
        self.assertFalse(await store_a.claim(event("acc_001")))

    async def test_pending_events_are_only_this_accounts_events(self):
        store_a = PostgresProcessedMessageStore(self.db_a)
        store_b = PostgresProcessedMessageStore(self.db_b)
        await store_a.publish(event("acc_001", "cho A"))
        await store_b.publish(event("acc_002", "cho B"))
        self.assertEqual([e.message for e in await store_a.pending()], ["cho A"])
        self.assertEqual([e.message for e in await store_b.pending()], ["cho B"])

    async def test_a_send_signature_claimed_by_one_account_leaves_the_other_free(self):
        signature = "tiktok_web|conv|m-1|reply"
        claim_a = await self.a.tasks.claim_send_task(
            signature=signature, task_id="task-a", platform="tiktok_web",
            conversation_id=SHARED_CONVERSATION, inbound_message_id="m-1",
        )
        claim_b = await self.b.tasks.claim_send_task(
            signature=signature, task_id="task-b", platform="tiktok_web",
            conversation_id=SHARED_CONVERSATION, inbound_message_id="m-1",
        )
        self.assertTrue(claim_a.granted)
        self.assertTrue(
            claim_b.granted,
            "account B's reply was blocked by account A's identical signature",
        )

    async def test_conversation_state_is_separate_per_account(self):
        from models.customer_state import CustomerState

        store_a = PostgresConversationStore(self.db_a)
        store_b = PostgresConversationStore(self.db_b)
        await store_a.save_state(CustomerState(
            conversation_id=SHARED_CONVERSATION, customer_id=SHARED_CUSTOMER,
            stage="ordering",
        ))
        await store_b.save_state(CustomerState(
            conversation_id=SHARED_CONVERSATION, customer_id=SHARED_CUSTOMER,
            stage="browsing",
        ))
        self.assertEqual((await store_a.get_state(SHARED_CONVERSATION)).stage, "ordering")
        self.assertEqual((await store_b.get_state(SHARED_CONVERSATION)).stage, "browsing")


class CrossAccountAccessIsBlockedTests(unittest.IsolatedAsyncioTestCase):
    """The boundary checks refuse rather than silently reading the wrong rows."""

    def setUp(self):
        self.database = make_database(self, account_id="acc_001")
        self.scope = RepositoryScope(self.database, "acc_001")

    async def test_a_task_belonging_to_another_account_is_refused(self):
        foreign = Task(
            id="t-foreign", account_id="acc_002",
            capability="messaging.scan_inbox", platform="tiktok_web",
        )
        with self.assertRaises(AccountIsolationError):
            await self.scope.tasks.submit(foreign)
        with self.assertRaises(AccountIsolationError):
            await self.scope.tasks.ensure_task(foreign)

    async def test_an_event_belonging_to_another_account_is_refused(self):
        store = PostgresProcessedMessageStore(self.database)
        with self.assertRaises(AccountIsolationError):
            await store.claim(event("acc_002"))

    def test_a_scope_cannot_reinterpret_another_accounts_database(self):
        with self.assertRaises(AccountIsolationError):
            RepositoryScope(self.database, "acc_999")

    def test_a_scope_without_an_account_is_refused(self):
        with self.assertRaises(AccountIsolationError):
            RepositoryScope(self.database, "")

    def test_every_repository_in_a_scope_carries_the_same_account(self):
        for name in ("tasks", "conversations", "customers", "processed_messages",
                     "escalations"):
            self.assertEqual(
                getattr(self.scope, name).account_id, "acc_001",
                f"{name} was not bound to the scope's account",
            )


class NoUnscopedQueryTests(unittest.TestCase):
    """Point 23: the codebase must contain no account-blind statement."""

    def test_no_sql_statement_touches_account_data_without_an_account_filter(self):
        findings = audit_project()
        report = "\n".join(
            f"{filename}:{line}: {sql[:120]}"
            for filename, rows in findings.items()
            for line, sql in rows
        )
        self.assertEqual(findings, {}, f"unscoped SQL found:\n{report}")


if __name__ == "__main__":
    unittest.main()
