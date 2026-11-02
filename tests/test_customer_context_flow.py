"""Credential-free tests for remembering a customer across replies.

No API key is used: the analyzer, generator and router are stubs, and the
storage layer runs against a real PostgreSQL schema, created per test. What is
verified is the wiring - that `PipelineReplyProvider` reads the stored context
before answering and writes back what the customer stated.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.message_analyzer import MessageAnalyzer
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from tests.support import make_database
from models.analysis import MessageAnalysis
from models.customer_state import DEFAULT_CUSTOMER_STAGES
from models.outbound_message import Attachment
from models.reply import ReplyResult
from memory.conversation_store import PostgresConversationStore


THREAD = "0:1:6981093337449300994:7278178670136558593"

SCHEMA = Path(__file__).resolve().parents[1] / "storage" / "schema.sql"


class RecordingAnalyzer:
    """Returns a scripted analysis and records the context it was given."""

    def __init__(self, *analyses):
        self.analyses = list(analyses)
        self.customer_contexts = []
        self.recent_messages = []

    async def analyze(self, event, state, recent_messages, customer_context=None):
        self.customer_contexts.append(customer_context or {})
        self.recent_messages.append(list(recent_messages))
        analysis = self.analyses.pop(0) if len(self.analyses) > 1 else self.analyses[0]
        return analysis.model_copy(deep=True)


class StaticAnalysisLlm:
    model_name = "test"

    def __init__(self, result):
        self.result = result

    async def generate_structured(self, _prompt, _schema):
        return self.result.model_copy(deep=True)


class StubGenerator:
    def __init__(self, text):
        self.text = text
        self.states = []

    async def generate(self, event, state, analysis, tool_result=None, attachments=(), unavailable_products=(), notes=()):
        self.states.append(state)
        return ReplyResult(text=self.text, should_send=True, model="stub")


def analysis(**overrides) -> MessageAnalysis:
    payload = {
        "intent": "ask_product",
        "next_stage": "asking_product",
        "confidence": 0.9,
        "can_reply_directly": True,
    }
    payload.update(overrides)
    return MessageAnalysis(**payload)


def context_for(text, message_id, *, history=()) -> ConversationContext:
    return ConversationContext(
        platform="tiktok_web",
        conversation_key=THREAD,
        message_id=message_id,
        message_text=text,
        customer_name="okeoki03",
        history=list(history),
        pending_messages=[{"id": message_id, "text": text, "index": 0}],
    )


class CustomerContextPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.store = PostgresConversationStore(self.database)
        self.generator = StubGenerator("Dạ shop ghi nhận thông tin của bạn ạ")

    def _provider(self, analyzer):
        return PipelineReplyProvider(
            analyzer=analyzer,
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=self.generator,
            reply_validator=ReplyValidator(1000),
            conversation_store=self.store,
        )

    def _rows(self, table):
        return [
            dict(row) for row in
            self.database.conn.execute(f"SELECT * FROM {table}").fetchall()
        ]

    async def test_stated_facts_are_written_to_profile_and_facts(self):
        analyzer = RecordingAnalyzer(analysis(
            customer_facts={"name": "Mạnh", "phone": "0912345678"}
        ))
        decision = await self._provider(analyzer).reply(
            context_for("Mình tên Mạnh, sđt 0912345678", "m1")
        )

        self.assertTrue(decision.should_send)
        facts = {row["fact_type"]: row["fact_value"] for row in self._rows("customer_facts")}
        self.assertEqual(facts, {"name": "Mạnh", "phone": "0912345678"})

        profile = self._rows("customer_profile")[0]
        self.assertEqual(profile["conversation_id"], THREAD)
        self.assertEqual(profile["display_name"], "Mạnh")
        self.assertEqual(profile["phone"], "0912345678")
        self.assertEqual(profile["username"], "okeoki03")
        customer = self._rows("account_customers")[0]
        self.assertEqual(customer["customer_id"], THREAD)
        self.assertEqual(customer["username"], "okeoki03")
        self.assertEqual(customer["metadata"], '{"display_name": "okeoki03"}')

    async def test_a_later_reply_reads_back_the_stored_customer(self):
        first = RecordingAnalyzer(analysis(customer_facts={"phone": "0912345678"}))
        await self._provider(first).reply(context_for("sđt mình 0912345678", "m1"))

        # A fresh provider: nothing survives in memory, only in the database.
        second = RecordingAnalyzer(analysis())
        await self._provider(second).reply(context_for("Còn hàng không?", "m2"))

        given = second.customer_contexts[0]
        self.assertEqual(given["customer_profile"]["phone"], "0912345678")
        self.assertEqual(given["customer_facts"]["phone"]["value"], "0912345678")

    async def test_customer_state_survives_between_replies(self):
        analyzer = RecordingAnalyzer(analysis(next_stage="asking_price"))
        await self._provider(analyzer).reply(context_for("Giá bao nhiêu?", "m1"))
        await self._provider(analyzer).reply(context_for("Còn size M không?", "m2"))

        state = await self.store.get_state(THREAD)
        self.assertEqual(state.stage, "asking_price")
        self.assertEqual(state.message_count, 2)
        # The second run started from the stored state, not from a blank one.
        self.assertEqual(self.generator.states[1].message_count, 2)

    async def test_the_visible_thread_is_stored_for_later_context(self):
        analyzer = RecordingAnalyzer(analysis())
        await self._provider(analyzer).reply(context_for(
            "Còn hàng không?", "m2",
            history=[
                {"id": "m0", "text": "Chào shop", "direction": "INBOUND"},
                {"id": "m1", "text": "Dạ shop nghe ạ", "direction": "OUTBOUND"},
                {"id": "m2", "text": "Còn hàng không?", "direction": "INBOUND"},
            ],
        ))

        stored = self._rows("conversation_messages")
        self.assertEqual(len(stored), 3)
        self.assertEqual({row["conversation_id"] for row in stored}, {THREAD})
        self.assertEqual(
            [row["direction"] for row in stored],
            ["INBOUND", "OUTBOUND", "INBOUND"],
        )

    async def test_re_storing_the_same_thread_does_not_duplicate_it(self):
        analyzer = RecordingAnalyzer(analysis())
        history = [
            {"id": "m1", "text": "Chào shop", "direction": "INBOUND"},
            {"id": "m2", "text": "Còn hàng không?", "direction": "INBOUND"},
        ]
        provider = self._provider(analyzer)
        await provider.reply(context_for("Còn hàng không?", "m2", history=history))
        await provider.reply(context_for("Còn hàng không?", "m2", history=history))

        self.assertEqual(len(self._rows("conversation_messages")), 2)

    async def test_low_confidence_facts_are_not_stored(self):
        analyzer = RecordingAnalyzer(analysis(
            confidence=0.5, customer_facts={"phone": "0912345678"}
        ))
        await self._provider(analyzer).reply(context_for("chắc là 0912345678", "m1"))

        self.assertEqual(self._rows("customer_facts"), [])
        # DOM identity is trusted independently from Gemini fact confidence.
        customer = self._rows("account_customers")[0]
        self.assertEqual(customer["username"], "okeoki03")
        profile = self._rows("customer_profile")[0]
        self.assertEqual(profile["username"], "okeoki03")

    async def test_customer_identity_updates_without_erasing_existing_metadata(self):
        repository = self.store.context_repository
        repository.upsert_customer_identity(
            THREAD,
            username="old_name",
            display_name="Old Name",
            platform_user_id="user-123",
            metadata={"source": "dom"},
        )
        repository.upsert_customer_identity(
            THREAD,
            username="new_name",
            display_name="New Name",
        )
        self.database.conn.commit()

        customer = self._rows("account_customers")[0]
        self.assertEqual(customer["username"], "new_name")
        self.assertEqual(customer["platform_user_id"], "user-123")
        self.assertEqual(
            customer["metadata"],
            '{"source": "dom", "display_name": "New Name"}',
        )
        profile = self._rows("customer_profile")[0]
        self.assertEqual(profile["username"], "new_name")
        self.assertEqual(profile["display_name"], "New Name")

    async def test_explicit_contact_is_stored_even_when_intent_confidence_is_low(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="unknown", next_stage="human_required",
                confidence=0.2, requires_human=True, can_reply_directly=False,
            )),
            registered_intents=("unknown",),
            registered_stages=DEFAULT_CUSTOMER_STAGES,
        )

        await self._provider(analyzer).reply(context_for(
            "Địa chỉ: 25 Nguyễn Trãi, Quận 1; "
            "email: TEST@example.com; Zalo: 0912 345 678",
            "m-contact",
        ))

        facts = {
            row["fact_type"]: (row["fact_value"], row["confidence"])
            for row in self._rows("customer_facts")
        }
        self.assertEqual(facts, {
            "address": ("25 Nguyễn Trãi, Quận 1", 1.0),
            "email": ("test@example.com", 1.0),
            "phone": ("0912345678", 1.0),
        })
        profile = self._rows("customer_profile")[0]
        self.assertEqual(profile["address"], "25 Nguyễn Trãi, Quận 1")
        self.assertEqual(profile["email"], "test@example.com")
        self.assertEqual(profile["phone"], "0912345678")
        remembered = await self.store.context(THREAD)
        self.assertEqual(remembered["customer_profile"]["address"], profile["address"])
        self.assertEqual(remembered["customer_profile"]["email"], profile["email"])
        self.assertEqual(
            remembered["customer_facts"]["phone"]["source_message_id"],
            "m-contact",
        )

    async def test_a_repeated_reply_is_rejected_from_stored_history(self):
        analyzer = RecordingAnalyzer(analysis())
        provider = self._provider(analyzer)
        first = await provider.reply(context_for("Còn hàng không?", "m1"))

        # A fresh provider has no in-memory record of what was already said;
        # the duplicate is caught from the stored thread instead.
        repeated = self._provider(RecordingAnalyzer(analysis()))
        second = await repeated.reply(context_for("Còn hàng không?", "m2", history=[
            {"id": "m1", "text": first.text, "direction": "OUTBOUND"},
        ]))

        self.assertTrue(first.should_send)
        self.assertFalse(second.should_send)
        self.assertEqual(second.reason, "duplicate_reply")

    async def test_known_address_generation_mistake_gets_a_safe_reply(self):
        analyzer = RecordingAnalyzer(analysis())
        provider = PipelineReplyProvider(
            analyzer=analyzer,
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=StubGenerator(
                "Bạn vui lòng cung cấp địa chỉ giao hàng giúp shop nhé."
            ),
            reply_validator=ReplyValidator(1000),
            conversation_store=self.store,
        )

        decision = await provider.reply(context_for(
            "Địa chỉ: số 10 đường ABC, Hà Nội", "m-address",
            history=[{
                "id": "m-address",
                "text": "Địa chỉ: số 10 đường ABC, Hà Nội",
                "direction": "INBOUND",
            }],
        ))

        self.assertTrue(decision.should_send, decision.reason)
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.model, "verified-context-fallback")
        self.assertIn("đã nhận được thông tin nơi nhận hàng", decision.text.casefold())
        self.assertNotIn("cung cấp địa chỉ", decision.text.casefold())


class SentReplyIsRecordedTests(unittest.IsolatedAsyncioTestCase):
    """A delivered reply is written down when it is sent, not a scan later."""

    def setUp(self):
        self.database = make_database(self)
        self.store = PostgresConversationStore(self.database)
        self.provider = PipelineReplyProvider(
            analyzer=RecordingAnalyzer(analysis()),
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=StubGenerator("Dạ shop còn hàng ạ"),
            reply_validator=ReplyValidator(1000),
            conversation_store=self.store,
        )

    def _outbound(self):
        return [
            row["text"] for row in self.database.conn.execute(
                "SELECT text FROM conversation_messages "
                "WHERE account_id = %s AND conversation_id = %s AND direction = 'OUTBOUND'",
                ("acc_test", THREAD),
            ).fetchall()
        ]

    async def test_a_sent_reply_is_stored_immediately(self):
        context = context_for("Còn hàng không?", "m1")
        decision = await self.provider.reply(context)
        await self.provider.record_sent(context, decision.text)

        self.assertEqual(self._outbound(), ["Dạ shop còn hàng ạ"])

    async def test_the_same_reply_seen_later_in_the_thread_is_not_duplicated(self):
        context = context_for("Còn hàng không?", "m1")
        decision = await self.provider.reply(context)
        await self.provider.record_sent(context, decision.text)

        # The next scan reads the thread, which now renders that same reply.
        await self.provider.reply(context_for("Giá bao nhiêu?", "m2", history=[
            {"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"},
            {"id": "m2", "text": decision.text, "direction": "OUTBOUND"},
        ]))

        self.assertEqual(self._outbound(), ["Dạ shop còn hàng ạ"])

    async def test_a_recorded_reply_blocks_repeating_itself(self):
        context = context_for("Còn hàng không?", "m1")
        first = await self.provider.reply(context)
        await self.provider.record_sent(context, first.text)

        second = await self.provider.reply(context_for("Còn hàng không?", "m2"))

        self.assertTrue(first.should_send)
        self.assertFalse(second.should_send)
        self.assertEqual(second.reason, "duplicate_reply")

    async def test_an_empty_reply_is_never_recorded(self):
        await self.provider.record_sent(context_for("Còn hàng?", "m1"), "   ")

        self.assertEqual(self._outbound(), [])

    def _media_rows(self, direction):
        return [
            json.loads(row["media_json"]) for row in self.database.conn.execute(
                "SELECT media_json FROM conversation_messages "
                "WHERE account_id = %s AND conversation_id = %s AND direction = %s "
                "AND media_json IS NOT NULL",
                ("acc_test", THREAD, direction),
            ).fetchall()
        ]

    async def test_a_sent_attachment_is_stored_alongside_the_reply(self):
        context = context_for("Cho mình xem sản phẩm ạ", "m1")
        attachment = Attachment(kind="image", path="/data/products/sp001/a.jpg", name="a.jpg")

        await self.provider.record_sent(context, "Đây là sản phẩm ạ", (attachment,))

        self.assertEqual(
            self._media_rows("OUTBOUND"),
            [[{"kind": "image", "path": "/data/products/sp001/a.jpg", "name": "a.jpg"}]],
        )

    async def test_an_image_only_attachment_with_no_caption_is_still_recorded(self):
        context = context_for("Cho mình xem ảnh", "m1")
        attachment = Attachment(kind="image", path="/data/products/sp001/a.jpg")

        await self.provider.record_sent(context, "", (attachment,))

        self.assertEqual(len(self._media_rows("OUTBOUND")), 1)

    async def test_persist_thread_stores_an_inbound_image_even_with_no_text(self):
        """A customer's screenshot has no caption - it must not be dropped
        for lack of text the way a media-only message used to be."""
        media = [{"kind": "image", "src": "https://p.tiktokcdn.com/photo.jpg",
                  "width": 300, "height": 300}]
        context = context_for("Còn hàng không?", "m1", history=[
            {"id": "m1", "text": "", "direction": "INBOUND", "index": 0, "media": media},
        ])

        await self.provider.persist_thread(context)

        self.assertEqual(self._media_rows("INBOUND"), [media])


class WithoutStoreTests(unittest.IsolatedAsyncioTestCase):
    """The provider still runs with no database at all - dry runs and tests."""

    async def test_known_address_is_acknowledged_instead_of_handed_off(self):
        item = analysis()
        item.verified_customer_context = {
            "recent_messages": [{
                "direction": "INBOUND",
                "text": "Nhận hàng tại số 10 đường ABC, Hà Nội",
            }]
        }
        provider = PipelineReplyProvider(
            analyzer=RecordingAnalyzer(item),
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=StubGenerator(
                "Bạn vui lòng cung cấp địa chỉ giao hàng giúp shop nhé."
            ),
            reply_validator=ReplyValidator(1000),
        )

        decision = await provider.reply(context_for(
            "Nhận hàng tại số 10 đường ABC, Hà Nội", "m-address"
        ))

        self.assertTrue(decision.should_send, decision.reason)
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.model, "verified-context-fallback")
        self.assertIn("đã nhận được thông tin nơi nhận hàng", decision.text.casefold())

    async def test_reply_works_without_a_conversation_store(self):
        provider = PipelineReplyProvider(
            analyzer=RecordingAnalyzer(analysis()),
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=StubGenerator("Dạ còn hàng ạ"),
            reply_validator=ReplyValidator(1000),
        )
        decision = await provider.reply(context_for("Còn hàng không?", "m1"))

        self.assertTrue(decision.should_send)
        self.assertEqual(decision.text, "Dạ còn hàng ạ")

    async def test_recorded_reply_is_not_repeated_without_a_store(self):
        provider = PipelineReplyProvider(
            analyzer=RecordingAnalyzer(analysis()),
            router=MessageRouter(0.65, []),
            state_manager=CustomerStateManager(DEFAULT_CUSTOMER_STAGES),
            reply_generator=StubGenerator("Dạ còn hàng ạ"),
            reply_validator=ReplyValidator(1000),
        )
        first_context = context_for("Còn hàng không?", "m1")
        first = await provider.reply(first_context)
        await provider.record_sent(first_context, first.text)

        second = await provider.reply(context_for("Giá bao nhiêu?", "m2"))

        self.assertFalse(second.should_send)
        self.assertEqual(second.reason, "duplicate_reply")


if __name__ == "__main__":
    unittest.main()
