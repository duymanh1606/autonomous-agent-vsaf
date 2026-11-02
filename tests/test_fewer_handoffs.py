"""Cases that used to hand a conversation to a person and now do not.

Each of these was a question the agent could answer, or a failure it could
retry, handed to staff anyway:

1. a send that failed before Send was pressed - nothing reached the customer,
   so a later scan retries it (only a run of these reaches a person);
2. a product name the catalogue does not know - the agent names what the shop
   sells and asks which one was meant;
3. a draft ReplyValidator refused for a fixable wording reason - one rewrite,
   told what was wrong, before a person;
4. a photo/video the product has no file for - said in words;
and the escalation row now keeps the analysis, so a handoff can be explained.

What stays with a person is tested alongside each: a lookup that broke, a size
the product has no data for, an unverified send, a refusal that is not about
wording.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_language import handoff_ack, is_wrong_language
from agent.reply_service import ConversationContext, PipelineReplyProvider, ReplyDecision
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from models.message_delivery import MessageSendState
from models.reply import ReplyResult
from models.result import ActionResult
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.messaging import ReplyMessageSkill
from platforms.tiktok.scan_outcomes import MAX_UNANSWERED_REVISITS, outcome_for
from platforms.tiktok.screens import TikTokScreen
from storage.product_repository import ProductRepository
from tests.test_inbox_scan_flow import (
    RecordingBus,
    RecordingEscalation,
    StubContext,
    StubReplyProvider,
    StubReplySkill,
    observation_for,
    scan_task,
)
from tests.test_reply_pipeline_parity import STAGES, StubAnalyzer, StubStore, analysis
from tests.test_sales_playbook_support import FactsStore
from tools.business import register_business_services
from tools.registry import BusinessToolRegistry

CALCIUM = {"name": "Calcium Gold", "price": 124000, "stock": 50}
MILK = {"name": "Sữa Hạt Óc Chó", "price": 98000, "stock": 20}


def catalogue(test, products):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for product_id, product in products.items():
        (root / product_id).mkdir()
        (root / product_id / "product.json").write_text(
            json.dumps({"id": product_id, **product}), encoding="utf-8")
    return ProductRepository(root)


class ScriptedGenerator:
    """Returns the drafts in order (the last one repeats) and records calls."""

    reply_language = "vi"

    def __init__(self, *drafts):
        self.drafts = list(drafts) or ["Dạ shop nghe ạ"]
        self.calls = []

    async def generate(self, event, state, analysis, tool_result=None,
                       attachments=(), unavailable_products=(), notes=()):
        self.calls.append({"tool_result": tool_result, "notes": tuple(notes)})
        text = self.drafts[min(len(self.calls), len(self.drafts)) - 1]
        return ReplyResult(text=text, should_send=True, model="stub")


def context(text):
    return ConversationContext(
        platform="tiktok_web", conversation_key="thread-1", message_id="m1",
        message_text=text, customer_name="khách",
        history=[{"id": "m1", "text": text, "direction": "INBOUND"}],
    )


class PipelineHandoffTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, repository, result, generator, *, media=("image", "video")):
        tools = BusinessToolRegistry(["get_product", "check_inventory"])
        if repository is not None:
            register_business_services(tools, product=repository, inventory=repository)
        return PipelineReplyProvider(
            analyzer=StubAnalyzer(result),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=generator,
            reply_validator=ReplyValidator(500),
            conversation_store=StubStore(),
            business_tools=tools,
            product_lookup=ProductLookupPolicy(True, catalogue=repository),
            catalogue=repository,
            enabled_media_kinds=media,
        )

    # ------------------------------------------------- 2. unknown product
    async def test_an_unknown_product_name_is_asked_about_not_handed_over(self):
        generator = ScriptedGenerator("Dạ shop chưa có sản phẩm đó, bạn hỏi Calcium Gold hay Sữa Hạt Óc Chó ạ?")
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM, "milk": MILK}),
            analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "Bánh quy bơ"}),
            generator,
        ).reply(context("Bánh quy bơ giá bao nhiêu?"))

        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)
        sent_to_model = generator.calls[0]["tool_result"].data
        self.assertEqual(sent_to_model["product_not_found"], "Bánh quy bơ")
        self.assertEqual(sorted(sent_to_model["products_on_sale"]), ["Calcium Gold", "Sữa Hạt Óc Chó"])
        self.assertTrue(any("not in the shop's catalogue" in note for note in generator.calls[0]["notes"]))

    async def test_asking_about_an_unknown_product_still_may_not_quote_a_price(self):
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "Bánh quy bơ"}),
            ScriptedGenerator("Dạ bánh quy bơ giá 50.000đ ạ"),
        ).reply(context("Bánh quy bơ giá bao nhiêu?"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "unverified_price_claim")

    async def test_a_size_the_product_has_no_data_for_still_goes_to_a_person(self):
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="check_inventory", requires_tool=True, tool_name="check_inventory",
                     tool_args={"product_id": "cg", "size": "XL"}),
            ScriptedGenerator(),
        ).reply(context("Calcium Gold còn size XL không?"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "required_tool_unavailable")

    async def test_a_broken_lookup_still_goes_to_a_person(self):
        repository = catalogue(self, {"cg": CALCIUM})
        with mock.patch.object(repository, "resolve", side_effect=OSError("disk")):
            decision = await self.provider(
                repository,
                analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                         tool_args={"product_id": "Bánh quy bơ"}),
                ScriptedGenerator(),
            ).reply(context("Bánh quy bơ giá bao nhiêu?"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "required_tool_unavailable")

    async def test_a_shop_with_nothing_on_sale_still_goes_to_a_person(self):
        decision = await self.provider(
            catalogue(self, {"cg": {**CALCIUM, "is_active": False}}),
            analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "Bánh quy bơ"}),
            ScriptedGenerator(),
        ).reply(context("Bánh quy bơ giá bao nhiêu?"))

        self.assertTrue(decision.requires_human)

    # ---------------------------------------------------- 4. missing media
    async def test_a_product_without_a_photo_is_answered_in_words(self):
        generator = ScriptedGenerator("Dạ Calcium Gold hiện shop chưa có ảnh, giá 124.000đ ạ")
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="ask_product", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "cg"}, requested_media=["image"]),
            generator,
        ).reply(context("Cho mình xem ảnh Calcium Gold"))

        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.attachments, ())
        self.assertTrue(any("none on file" in note for note in generator.calls[0]["notes"]))

    async def test_a_photo_request_naming_no_product_asks_which(self):
        generator = ScriptedGenerator("Dạ bạn muốn xem ảnh sản phẩm nào ạ?")
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM, "milk": MILK}),
            analysis(intent="ask_product", requested_media=["image"]),
            generator,
        ).reply(context("Cho mình xem ảnh"))

        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)
        self.assertTrue(any("of which product" in note for note in generator.calls[0]["notes"]))

    async def test_a_media_kind_the_shop_turned_off_still_goes_to_a_person(self):
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="ask_product", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "cg"}, requested_media=["video"]),
            ScriptedGenerator(), media=("image",),
        ).reply(context("Cho mình xem video Calcium Gold"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "requested_video_unavailable")

    # ------------------------------------------------- 3. one rewrite
    async def test_a_refused_draft_is_rewritten_once_and_sent(self):
        generator = ScriptedGenerator(
            "Dạ Calcium Gold giá 99.000đ ạ",       # a price nothing verified
            "Dạ Calcium Gold giá 124.000đ ạ",
        )
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "cg"}),
            generator,
        ).reply(context("Calcium Gold giá bao nhiêu?"))

        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.text, "Dạ Calcium Gold giá 124.000đ ạ")
        self.assertEqual(len(generator.calls), 2)
        self.assertTrue(any("not in Verified tool result" in note for note in generator.calls[1]["notes"]))

    async def test_a_rewrite_that_is_still_wrong_goes_to_a_person(self):
        generator = ScriptedGenerator("Dạ Calcium Gold giá 99.000đ ạ")
        decision = await self.provider(
            catalogue(self, {"cg": CALCIUM}),
            analysis(intent="ask_price", requires_tool=True, tool_name="get_product",
                     tool_args={"product_id": "cg"}),
            generator,
        ).reply(context("Calcium Gold giá bao nhiêu?"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "unverified_price_claim")
        self.assertEqual(len(generator.calls), 2)

    async def test_a_refusal_that_is_not_about_wording_is_not_rewritten(self):
        generator = ScriptedGenerator("Dạ shop nghe ạ")
        with mock.patch.object(
            ReplyValidator, "validate",
            return_value=SimpleNamespace(allowed=False, reason="human_review_required"),
        ):
            decision = await self.provider(
                catalogue(self, {"cg": CALCIUM}), analysis(intent="faq"), generator,
            ).reply(context("Shop ơi"))

        self.assertTrue(decision.requires_human)
        self.assertEqual(len(generator.calls), 1)


class HandoffAsksForContactTests(unittest.IsolatedAsyncioTestCase):
    """A handoff asks for a phone number - only when the shop has none."""

    async def handoff(self, *, store=None, result=None):
        provider = PipelineReplyProvider(
            analyzer=StubAnalyzer(result or analysis(intent="complaint")),
            router=MessageRouter(human_intents=("complaint",)),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=ScriptedGenerator(),
            reply_validator=ReplyValidator(500),
            conversation_store=store or StubStore(),
        )
        return await provider.reply(context("Hàng bị vỡ rồi shop"))

    async def test_without_a_phone_the_handoff_asks_for_one(self):
        decision = await self.handoff()
        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.text, handoff_ack("vi", ask_contact=True))
        self.assertIn("số điện thoại", decision.text.splitlines()[-1])

    async def test_a_stored_phone_is_not_asked_for_again(self):
        decision = await self.handoff(store=FactsStore({"phone": "0912345678"}))
        self.assertEqual(decision.text, handoff_ack("vi"))

    async def test_a_phone_in_this_message_is_not_asked_for(self):
        decision = await self.handoff(result=analysis(
            intent="complaint", customer_facts={"phone": "0912345678"},
        ))
        self.assertEqual(decision.text, handoff_ack("vi"))

    def test_both_languages_carry_the_line_in_their_own_script(self):
        for code in ("vi", "my"):
            with self.subTest(code=code):
                text = handoff_ack(code, ask_contact=True)
                self.assertEqual(text.splitlines()[0], handoff_ack(code))
                self.assertFalse(is_wrong_language(code, text))


class SendNotAttemptedTests(unittest.IsolatedAsyncioTestCase):
    """1. A send that failed before Send was pressed is retried, not escalated."""

    THREAD = [{"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"}]

    def setUp(self):
        from services.handoff import HumanHandoff

        self.escalation = RecordingEscalation()
        self.handoff = HumanHandoff(
            account_id="acc_test", escalation_service=self.escalation, events=RecordingBus(),
        )

    def _context(self):
        context = StubContext(
            observation_for(TikTokScreen.CONVERSATION, messages=self.THREAD, active="customer"),
            {
                "platform.list_unread_conversations": ActionResult(
                    success=True, data={"tool": {"conversations": []}}
                ),
            },
        )
        context.platform = SimpleNamespace(state_verifier=SimpleNamespace())
        return context

    def _skill(self, send_result):
        return InboxScanSkill(
            reply_provider=StubReplyProvider(
                ReplyDecision(text="Dạ còn hàng ạ", should_send=True, model="stub")
            ),
            reply_skill=StubReplySkill(send_result),
            handoff=self.handoff,
        )

    async def test_a_failure_before_send_is_queued_for_the_next_scan(self):
        skill = self._skill(ActionResult(
            success=False, error="recipient_mismatch: active chat does not match",
            data={"status": "recipient_mismatch",
                  "send_status": MessageSendState.NOT_SENT.value},
        ))

        result = await skill.execute(scan_task(), self._context())
        await asyncio.sleep(0)

        self.assertEqual(result.data["active_conversation"]["status"], "send_not_attempted")
        self.assertEqual(self.escalation.rows, [])
        self.assertIn("customer", skill._unanswered)

    async def test_only_a_run_of_them_reaches_a_person(self):
        skill = self._skill(ActionResult(
            success=False, error="TargetNotFound('conversation_item')",
            data={"send_status": MessageSendState.NOT_SENT.value},
        ))
        for _ in range(MAX_UNANSWERED_REVISITS + 1):
            await skill.execute(scan_task(), self._context())
        await asyncio.sleep(0)

        reasons = [row[2] for row in self.escalation.rows]
        self.assertEqual(len(reasons), 1)
        self.assertIn("send_not_attempted", reasons[0])

    async def test_an_unverified_send_is_still_escalated_at_once(self):
        skill = self._skill(ActionResult(
            success=False, error="send was not confirmed",
            data={"send_status": MessageSendState.UNCERTAIN.value},
        ))

        result = await skill.execute(scan_task(), self._context())
        await asyncio.sleep(0)

        self.assertEqual(result.data["active_conversation"]["status"], "reconcile_required")
        self.assertEqual(
            [row[2] for row in self.escalation.rows],
            ["delivery_uncertain_requires_reconciliation"],
        )

    def test_the_outcome_is_retried_with_the_ordinary_budget(self):
        outcome = outcome_for("send_not_attempted")
        self.assertFalse(outcome.settled)
        self.assertTrue(outcome.counts_attempt)
        self.assertEqual(outcome.retry_budget, MAX_UNANSWERED_REVISITS)


class AbortBeforeSendTests(unittest.IsolatedAsyncioTestCase):
    """Which aborts the messaging skill reports as provably not sent."""

    async def abort(self, *, cleanup_ok=True, **kwargs):
        skill = ReplyMessageSkill()
        skill._cleanup_draft = mock.AsyncMock(return_value=ActionResult(
            success=cleanup_ok, error=None if cleanup_ok else "draft still there",
        ))
        result = await skill._abort_before_send(
            SimpleNamespace(), None, SimpleNamespace(id="t1"), "sig",
            ActionResult(success=False, error="recipient_mismatch: x", data={}),
            **kwargs,
        )
        return (result.data or {}).get("send_status")

    async def test_a_clean_abort_is_not_sent(self):
        self.assertEqual(await self.abort(), MessageSendState.NOT_SENT.value)

    async def test_a_file_that_may_have_gone_out_is_uncertain(self):
        self.assertEqual(await self.abort(may_have_sent=True), MessageSendState.UNCERTAIN.value)

    async def test_a_draft_left_behind_is_not_retried(self):
        self.assertIsNone(await self.abort(cleanup_ok=False))

    async def test_a_block_about_an_earlier_attempt_is_not_retried(self):
        self.assertIsNone(await self.abort(retry_safe=False))


class EscalationKeepsTheAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_analysis_reaches_the_escalation_row(self):
        from services.handoff import HumanHandoff

        seen = []

        class Escalation:
            async def escalate(self, event, state, reason, analysis=None):
                seen.append(analysis)

        result = analysis(intent="ask_price", requires_human=True)
        skill = InboxScanSkill(
            reply_provider=StubReplyProvider(
                ReplyDecision.human("routed_to_human:ask_price", analysis=result)
            ),
            reply_skill=StubReplySkill(ActionResult(success=True, data={})),
            handoff=HumanHandoff(
                account_id="acc_test", escalation_service=Escalation(), events=RecordingBus(),
            ),
        )
        context = StubContext(
            observation_for(
                TikTokScreen.CONVERSATION,
                messages=[{"id": "m1", "text": "Giá?", "direction": "INBOUND"}],
                active="customer",
            ),
            {"platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": []}})},
        )
        context.platform = SimpleNamespace(state_verifier=SimpleNamespace())

        await skill.execute(scan_task(), context)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].intent, "ask_price")


if __name__ == "__main__":
    unittest.main()
