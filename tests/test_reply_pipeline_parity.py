"""One chain, one behaviour: `PipelineReplyProvider` is the single
analyse -> route -> generate -> validate chain that answers customers.

A queue-driven path (`MessageController`) used to carry its own copy, and the
copies drifted: the unknown-intent guard and conversation summaries existed
only on the path that did not run. That path is gone; these tests pin the
behaviour that moved into the provider.

No API key: the analyzer, generator and store are stubs.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_language import (
    DEFAULT_REPLY_LANGUAGE,
    handoff_ack,
)
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from models.analysis import MessageAnalysis
from models.reply import ReplyResult


STAGES = ("new", "browsing", "asking_price", "human_required")


def analysis(**overrides) -> MessageAnalysis:
    data = {
        "intent": "faq",
        "next_stage": "browsing",
        "confidence": 0.9,
    }
    data.update(overrides)
    return MessageAnalysis(**data)


class StubAnalyzer:
    def __init__(self, result):
        self.result = result
        self.seen_history = []

    async def analyze(self, event, state, messages, customer_context=None):
        self.seen_history.append(list(messages))
        return self.result.model_copy(deep=True)


class StubGenerator:
    async def generate(self, event, state, analysis, tool_result=None, attachments=(), unavailable_products=(), notes=()):
        return ReplyResult(text="Dạ shop nghe ạ", should_send=True, model="stub")


class StubSummary:
    """Summarises on demand and records that it was asked."""

    source_limit = 40

    def __init__(self, due=True):
        self.due = due
        self.calls = 0

    def should_summarize(self, state):
        return self.due

    async def summarize(self, state, messages):
        self.calls += 1
        return "khách hỏi giá áo thun"


class StubStore:
    """Just enough conversation store to exercise the chain."""

    account_id = "acc_test"

    def __init__(self, state=None, recent=()):
        self.state = state
        self.recent_messages = list(recent)
        self.saved = []
        self.committed = []

    async def get_state(self, conversation_id):
        return self.state

    async def recent(self, conversation_id, limit):
        return self.recent_messages

    async def history(self, conversation_id, limit):
        return self.recent_messages

    async def context(self, conversation_id, limit):
        return {}

    async def merge_customer_facts(self, *args, **kwargs):
        return None

    async def last_outbound_text(self, conversation_id):
        return None

    async def save_state(self, state):
        self.state = state
        self.saved.append(state)

    async def append(self, message):
        self.recent_messages.append(message)


def provider(store, *, analyzer=None, summary=None, unknown_intent_limit=2):
    return PipelineReplyProvider(
        analyzer=analyzer or StubAnalyzer(analysis()),
        router=MessageRouter(),
        state_manager=CustomerStateManager(STAGES),
        reply_generator=StubGenerator(),
        reply_validator=ReplyValidator(500),
        conversation_store=store,
        summary_service=summary,
        unknown_intent_limit=unknown_intent_limit,
    )


def context(text="Còn hàng không shop", message_id="m1"):
    return ConversationContext(
        platform="tiktok_web",
        conversation_key="thread-1",
        message_id=message_id,
        message_text=text,
        customer_name="khách",
        history=[{"id": "m0", "text": text, "direction": "INBOUND"}],
    )


class CannedRepliesFollowTheShopsLanguage(unittest.IsolatedAsyncioTestCase):
    """`messaging.reply_language` is applied live by control/settings.py, which
    writes it onto the generator; the provider reads it from there, so a Myanmar
    workspace stops acknowledging handoffs in Vietnamese."""

    async def test_the_handoff_ack_is_written_in_burmese(self):
        store = StubStore()
        chain = provider(store, analyzer=StubAnalyzer(analysis(intent="unknown")))
        chain.reply_generator.reply_language = "my"

        await chain.reply(context(message_id="m1"))
        second = await chain.reply(context(message_id="m2"))

        self.assertEqual(second.model, "human-handoff-ack")
        self.assertTrue(second.requires_human)
        self.assertEqual(second.text, handoff_ack("my", ask_contact=True))
        self.assertNotIn("chuyển nhân viên", second.text)

    async def test_a_generator_without_a_language_keeps_the_default(self):
        store = StubStore()
        chain = provider(store, analyzer=StubAnalyzer(analysis(intent="unknown")))
        self.assertFalse(hasattr(chain.reply_generator, "reply_language"))

        await chain.reply(context(message_id="m1"))
        second = await chain.reply(context(message_id="m2"))

        self.assertEqual(second.text, handoff_ack(DEFAULT_REPLY_LANGUAGE, ask_contact=True))


class TheUnknownIntentGuardRunsOnBothPaths(unittest.IsolatedAsyncioTestCase):
    async def test_a_second_unparsed_message_goes_to_a_human(self):
        store = StubStore()
        chain = provider(store, analyzer=StubAnalyzer(analysis(intent="unknown")))

        first = await chain.reply(context(message_id="m1"))
        second = await chain.reply(context(message_id="m2"))

        # The first guess is allowed; a run of them is not.
        self.assertTrue(first.should_send)
        self.assertTrue(second.should_send)
        self.assertTrue(second.requires_human)
        self.assertEqual(second.model, "human-handoff-ack")
        self.assertIn("chuyển nhân viên", second.text)
        self.assertEqual(second.state.stage, "human_required")

    async def test_the_escalation_turn_does_not_double_count(self):
        """Regression guard: escalating on the turn that crosses the limit
        used to run `CustomerStateManager.transition` a second time to stamp
        `requires_human`/`stage`, which also recomputed `unknown_intent_count`
        off the already-incremented state and inflated it (2 -> 3 instead of
        staying 2). The stored count must equal exactly how many unparsed
        messages were actually seen."""
        store = StubStore()
        chain = provider(store, analyzer=StubAnalyzer(analysis(intent="unknown")))

        await chain.reply(context(message_id="m1"))
        second = await chain.reply(context(message_id="m2"))

        self.assertTrue(second.requires_human)
        self.assertEqual(store.state.unknown_intent_count, 2)

    async def test_an_understood_message_resets_the_run(self):
        store = StubStore()
        unknown = StubAnalyzer(analysis(intent="unknown"))
        chain = provider(store, analyzer=unknown)

        await chain.reply(context(message_id="m1"))
        unknown.result = analysis(intent="faq")          # this one parses
        await chain.reply(context(message_id="m2"))
        unknown.result = analysis(intent="unknown")
        third = await chain.reply(context(message_id="m3"))

        self.assertTrue(third.should_send)


class LongConversationsAreSummarised(unittest.IsolatedAsyncioTestCase):
    async def test_the_summary_runs_on_the_path_that_answers_customers(self):
        store = StubStore()
        summary = StubSummary(due=True)

        decision = await provider(store, summary=summary).reply(context())

        self.assertTrue(decision.should_send)
        self.assertEqual(summary.calls, 1)
        self.assertEqual(store.state.conversation_summary, "khách hỏi giá áo thun")

    async def test_a_failing_summary_never_costs_the_answer(self):
        class BrokenSummary(StubSummary):
            async def summarize(self, state, messages):
                raise RuntimeError("model unavailable")

        decision = await provider(StubStore(), summary=BrokenSummary()).reply(context())

        self.assertTrue(decision.should_send)


class TheInMemoryFallbacksStayBounded(unittest.IsolatedAsyncioTestCase):
    """Caches for the database-free path must not grow with every customer."""

    async def test_old_conversations_fall_out_of_the_cache(self):
        from agent.reply_service import MEMORY_CACHE_LIMIT

        chain = PipelineReplyProvider(
            analyzer=StubAnalyzer(analysis()),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=StubGenerator(),
            reply_validator=ReplyValidator(500),
        )
        for index in range(MEMORY_CACHE_LIMIT + 25):
            await chain.reply(ConversationContext(
                platform="tiktok_web",
                conversation_key=f"thread-{index}",
                message_id="m1",
                message_text="alo shop",
            ))

        self.assertEqual(len(chain._states), MEMORY_CACHE_LIMIT)
        self.assertEqual(len(chain._last_outbound), MEMORY_CACHE_LIMIT)
        # The newest conversation is the one still cached.
        self.assertIn(
            ("", f"thread-{MEMORY_CACHE_LIMIT + 24}"), chain._states
        )


if __name__ == "__main__":
    unittest.main()
