"""Voice messages and other types TikTok Web cannot show.

TikTok Web renders them as a placeholder sentence. That sentence used to reach
the model as if the customer had typed it. Now it is recognised (Vietnamese
and English UI), the model is never called for it, the configured line is
sent - in the shop's own language - and a person takes over with a
notification that says what happened.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.reply_language import unsupported_message_ack
from agent.reply_service import (
    UNSUPPORTED_MESSAGE_REASON, ConversationContext, PipelineReplyProvider,
)
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from agent.unsupported_message import is_unsupported_placeholder
from dashboard_api.notifications import NotificationStore
from tests.test_reply_pipeline_parity import STAGES, StubGenerator, StubStore

VI_PLACEHOLDER = (
    "[Loại tin nhắn này không được hỗ trợ. Hãy tải về ứng dụng TikTok để xem "
    "tin nhắn này.]"
)
EN_PLACEHOLDER = (
    "[This message type isn’t supported. Download the TikTok app to view "
    "this message.]"
)
CONFIGURED = "Vui lòng đợi trong giây lát, nhân viên sẽ trả lời bạn"


class PlaceholderRecognitionTests(unittest.TestCase):
    def test_both_ui_languages_are_recognised(self):
        for text in (
            VI_PLACEHOLDER, EN_PLACEHOLDER,
            "This message type is not supported. Download the TikTok app to view this message.",
            "  [loại TIN NHẮN này   không được hỗ trợ.]  ",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_unsupported_placeholder(text))

    def test_a_customer_talking_about_the_app_is_not_a_placeholder(self):
        for text in (
            "tải app TikTok ở đâu shop", "Shop gửi qua app được không",
            "Hi", "", "Video này không được hỗ trợ đổi trả à?",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_unsupported_placeholder(text))


class AcknowledgementLanguageTests(unittest.TestCase):
    def test_the_configured_line_is_used_for_its_own_language(self):
        self.assertEqual(unsupported_message_ack("vi", CONFIGURED), CONFIGURED)

    def test_a_myanmar_shop_never_sends_the_vietnamese_line(self):
        ack = unsupported_message_ack("my", CONFIGURED)
        self.assertNotEqual(ack, CONFIGURED)
        self.assertIn("ဝန်ထမ်း", ack)

    def test_unset_falls_back_to_the_languages_own_line(self):
        self.assertTrue(unsupported_message_ack("vi"))


class NeverCalledAnalyzer:
    async def analyze(self, *args, **kwargs):
        raise AssertionError("the model must not see TikTok's placeholder")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, analyzer=None):
        from tests.test_reply_pipeline_parity import StubAnalyzer, analysis
        return PipelineReplyProvider(
            analyzer=analyzer or StubAnalyzer(analysis()),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=StubGenerator(),
            reply_validator=ReplyValidator(500),
            conversation_store=StubStore(),
            unsupported_message_reply=CONFIGURED,
        )

    def context(self, *texts):
        return ConversationContext(
            platform="tiktok_web", conversation_key="thread-1", message_id="m9",
            message_text="\n".join(texts), customer_name="khách",
            history=[{"id": f"m{i}", "text": t, "direction": "INBOUND"}
                     for i, t in enumerate(texts)],
            pending_messages=[{"id": f"m{i}", "text": t, "index": i}
                              for i, t in enumerate(texts)],
        )

    async def test_a_voice_message_is_handed_to_a_person_with_the_configured_line(self):
        decision = await self.provider(NeverCalledAnalyzer()).reply(self.context(VI_PLACEHOLDER))

        self.assertTrue(decision.should_send)
        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.text, CONFIGURED)
        self.assertEqual(decision.reason, UNSUPPORTED_MESSAGE_REASON)

    async def test_an_english_placeholder_is_handled_the_same(self):
        decision = await self.provider(NeverCalledAnalyzer()).reply(self.context(EN_PLACEHOLDER))
        self.assertTrue(decision.requires_human)

    async def test_voice_sent_with_text_still_goes_to_a_person(self):
        """Nobody here can hear the voice part; answering the text alone
        could contradict what the customer said in it."""
        decision = await self.provider(NeverCalledAnalyzer()).reply(
            self.context("Shop ơi", VI_PLACEHOLDER),
        )
        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.text, CONFIGURED)

    async def test_ordinary_text_still_reaches_the_pipeline(self):
        decision = await self.provider().reply(self.context("Còn hàng không shop"))
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.reason, "generated")


class InboxScanEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The real inbox scan with the real pipeline: the configured line is
    sent first, then the conversation is escalated with this reason."""

    async def test_a_voice_message_is_acknowledged_then_escalated(self):
        from services.handoff import HumanHandoff
        from tests.test_inbox_scan_flow import (
            HumanHandoffTests, RecordingBus, RecordingEscalation, scan_task,
        )
        from models.result import ActionResult
        from platforms.tiktok.inbox_watcher import InboxScanSkill

        escalation, bus = RecordingEscalation(), RecordingBus()
        sent = []

        class ReplySkill:
            async def execute(self, task, context):
                sent.append((task.payload["response"], len(escalation.rows)))
                return ActionResult(success=True, data={
                    "decision": "complete_task", "send_status": "SENT",
                })

        skill = InboxScanSkill(
            reply_provider=PipelineTests.provider(None, NeverCalledAnalyzer()),
            reply_skill=ReplySkill(),
            handoff=HumanHandoff(account_id="acc_test", escalation_service=escalation,
                                 events=bus),
        )
        thread = [{"id": "v1", "text": VI_PLACEHOLDER, "direction": "INBOUND"}]
        await skill.execute(scan_task(), HumanHandoffTests._context(None, thread))

        # Sent before the escalation existed, so the policy gate let it through.
        self.assertEqual(sent, [(CONFIGURED, 0)])
        [(_, message_id, reason)] = escalation.rows
        self.assertEqual(message_id, "v1")
        self.assertEqual(reason, f"reply_withheld:{UNSUPPORTED_MESSAGE_REASON}")
        self.assertEqual([event for event, _ in bus.events], ["escalation.opened"])


class NotificationTests(unittest.TestCase):
    def test_the_escalation_reads_as_a_sentence_with_the_customer(self):
        store = NotificationStore("postgresql://unused")
        recorded = []
        store.record = lambda **kwargs: recorded.append(kwargs)

        store.from_event({
            "type": "escalation.opened", "account_id": "shop-a",
            "payload": {"conversation": "Lan", "conversation_id": "c1",
                        "reason": f"reply_withheld:{UNSUPPORTED_MESSAGE_REASON}"},
        })

        [notification] = recorded
        self.assertIn("tin nhắn thoại", notification["title"])
        self.assertIn("Lan", notification["title"])
        self.assertIn("app TikTok", notification["detail"])
        self.assertEqual(notification["severity"], "warn")


if __name__ == "__main__":
    unittest.main()
