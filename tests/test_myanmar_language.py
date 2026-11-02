"""A shop on `messaging.reply_language: my`, end to end where it is not
already pinned elsewhere.

tests/test_reply_language_guard.py covers the script check and the generator
retry, tests/test_message_logic_guards.py the Burmese media reader and kyat
prices. These cover what sits around them: the dashboard routes that choose
the language, the attachment line in the prompt, the address fallback, the
text-only fallback after a failed media send, and script-check edge cases.

Credential-free: no LLM, no Chrome, no Postgres.
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import dashboard_api.main as main
from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.reply_generator import ReplyGenerator
from agent.reply_language import (
    default_reply_style,
    REPLY_LANGUAGES,
    address_ack,
    is_wrong_language,
    language_label,
)
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from load_config import load_config
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.outbound_message import Attachment
from models.reply import ReplyResult
from models.tool import ToolResult
from platforms.tiktok.messaging import ReplyMessageSkill

BASE_DIR = Path(__file__).resolve().parents[1]


def _analysis(**overrides):
    data = {"intent": "ask_product", "next_stage": "asking_product", "confidence": 0.9}
    data.update(overrides)
    return MessageAnalysis(**data)


def _event(message):
    return MessageEvent(
        platform="tiktok_web", conversation_id="c1", customer_id="u1",
        message_id="m-1", message=message, timestamp=datetime.now(timezone.utc),
    )


class ScriptedLLM:
    model_name = "test-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class BurmeseScriptCheckEdgeCases(unittest.TestCase):
    def test_a_burmese_reply_naming_a_latin_product_is_not_wrong(self):
        """Product names stay in Latin letters; the Burmese around them is
        what makes the reply Burmese."""
        self.assertFalse(is_wrong_language("my", "Nike Air Zoom Mercurial ဖိနပ် ရှိပါတယ်ရှင်"))

    def test_the_myanmar_extension_blocks_count_as_burmese(self):
        # Myanmar Extended-A (Khamti Shan) and Extended-B (Shan Pali).
        self.assertFalse(is_wrong_language("my", "Hello customer ꩠꩡꩢ"))
        self.assertFalse(is_wrong_language("my", "Hello customer ꧠꧡ"))

    def test_a_price_in_myanmar_digits_alone_is_never_flagged(self):
        self.assertFalse(is_wrong_language("my", "၂၅၀၀၀ Ks"))

    def test_a_myanmar_digit_does_not_make_vietnamese_burmese(self):
        """One "၃" used to pass a whole Vietnamese sentence as Burmese."""
        self.assertTrue(is_wrong_language("my", "Thời gian giao hàng dự kiến khoảng ၃ ngày."))
        self.assertTrue(is_wrong_language("my", "Dạ shop gửi bạn ạ ။"))

    def test_a_burmese_reply_naming_a_vietnamese_product_is_burmese(self):
        self.assertFalse(is_wrong_language(
            "my", "Sữa Ông Thọ ရှိပါတယ်ရှင့်။ ဈေးနှုန်းက ၂၈၀၀၀ ကျပ်ပါ",
        ))

    def test_a_vietnamese_reply_naming_a_burmese_product_is_vietnamese(self):
        self.assertFalse(is_wrong_language(
            "vi", "Dạ sản phẩm ဖိနပ် hiện còn hàng, bạn muốn đặt mấy đôi ạ?",
        ))

    def test_vietnamese_written_decomposed_is_still_vietnamese(self):
        """TikTok text can arrive in NFD; the marks are separate characters."""
        import unicodedata
        text = unicodedata.normalize("NFD", "Dạ shop có nhiều loại giày đá bóng ạ.")
        self.assertFalse(is_wrong_language("vi", text))
        self.assertTrue(is_wrong_language("my", text))

    def test_another_non_latin_script_is_still_wrong_for_a_myanmar_shop(self):
        """The check is "is this Burmese", not "is this non-Latin": a Thai
        answer is as wrong as an English one."""
        self.assertTrue(is_wrong_language("my", "สวัสดีครับ ยินดีให้บริการครับ"))

    def test_a_burmese_reply_is_wrong_for_a_vietnamese_shop(self):
        """Burmese asat (U+103A) and virama (U+1039) are Unicode combining
        marks; the Vietnamese check used to take them for diacritics, so a
        `vi` shop answering in Burmese was never retried."""
        self.assertTrue(is_wrong_language("vi", "မင်္ဂလာပါ ဘာကူညီပေးရမလဲ"))


class BurmeseAttachmentPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_attachment_line_asks_for_burmese(self):
        """The sentence announcing the photo must be written in the shop's
        language - the prompt used to carry a Vietnamese example for it."""
        llm = ScriptedLLM([
            ReplyResult(text="ဓာတ်ပုံ ပို့ပေးလိုက်ပါတယ်ရှင်", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500, reply_language="my")

        await generator.generate(
            _event("ဓာတ်ပုံပို့ပေးပါ"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(requested_media=["image"]),
            attachments=(Attachment(kind="image", path="/tmp/a.jpg"),),
        )

        self.assertEqual(len(llm.prompts), 1)
        prompt = llm.prompts[0]
        self.assertIn("in Burmese (Myanmar) like the rest of the reply", prompt)
        self.assertIn("a product photo", prompt)
        self.assertNotIn("Shop gửi bạn ảnh mẫu", prompt)
        self.assertNotIn("in Vietnamese like the rest", prompt)


class BurmeseMediaReaderExtraCases(unittest.TestCase):
    def test_more_burmese_requests(self):
        cases = (
            # Latin loanword in capitals: casefolded before matching.
            ("VIDEO ပို့ပေးပါ", ["video"]),
            # "Show me the picture" with the short noun ပုံ.
            ("ပုံ ပြပေးပါ", ["image"]),
            # "Can I get a photo?"
            ("ဓာတ်ပုံ ရမလား", ["image"]),
            # Alternative video spelling, "I want to see".
            ("ဗွီဒီယို ကြည့်ချင်တယ်", ["video"]),
            # "I can't see the photo" - still waiting, so still a request.
            ("ဓာတ်ပုံ မမြင်ရဘူး", ["image"]),
            # "I have already sent the video" - inbound, not a request.
            ("ဗီဒီယို ပို့ပြီးပြီ", []),
            # "Don't give me the photo."
            ("ဓာတ်ပုံ မပေးပါနဲ့", []),
            # Nothing about media at all.
            ("ဈေးနှုန်း ဘယ်လောက်လဲ", []),
            # -ပုံ "way of", not a picture: how to use / how to order /
            # how to apply; ပုံစံ is style, ပုံမှန် is usual.
            ("အသုံးပြုပုံ သိချင်ပါတယ်", []),
            ("မှာပုံ ပြောပြပေးပါ", []),
            ("လိမ်းပုံ ပြပေးပါ", []),
            ("ပုံစံ ဘယ်လိုလဲ", []),
            ("ပုံမှန် ဘယ်နှရက်ကြာလဲ", []),
            # "Want to buy" with a picture mentioned is not a request for one.
            ("ဓာတ်ပုံထဲက ပစ္စည်း ဝယ်ချင်တယ်", []),
            # ပုံ on its own still means a picture.
            ("ပုံလေး ပို့ပေးပါ", ["image"]),
            ("ပစ္စည်းပုံ ပို့ပေးပါ", ["image"]),
            ("ပုံစံ ဓာတ်ပုံ ပို့ပေးပါ", ["image"]),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(MessageAnalyzer._explicit_media_requests(text), expected)


class BurmesePriceShorthandTests(unittest.TestCase):
    """သိန်း (lakh) is 100,000 kyat: "၃ သိန်း" is a 300,000 price claim."""

    def _validate(self, text, price):
        return ReplyValidator().validate(
            ReplyResult(text=text, should_send=True),
            _analysis(
                intent="ask_price", next_stage="asking_price",
                requires_tool=True, tool_name="get_product",
            ),
            tool_result=ToolResult(
                tool_name="get_product", success=True,
                data={"id": "sp1", "price": price, "stock": 5},
            ),
            last_outbound_text=None,
            attachments=(),
        )

    def test_a_lakh_price_matching_the_tool_is_allowed(self):
        self.assertTrue(self._validate("ဒီပစ္စည်း ၃ သိန်း ပါရှင်", 300000).allowed)
        self.assertTrue(self._validate("ဒီပစ္စည်း 1.5 သိန်း ပါရှင်", 150000).allowed)

    def test_a_lakh_price_the_tool_never_stated_is_rejected(self):
        result = self._validate("ဒီပစ္စည်း ၅ သိန်း ပါရှင်", 300000)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "unverified_price_claim")

    def test_an_invented_mmk_price_is_rejected(self):
        result = self._validate("ဒီပစ္စည်း 45000 MMK ပါရှင်", 25000)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "unverified_price_claim")


class BurmeseTextOnlyFallbackTests(unittest.TestCase):
    """After TikTok rejects the attachment, the text may go out alone - but
    never a text that promises the photo that did not go."""

    REJECTED = SimpleNamespace(error="attachment rejected by media button")

    def test_a_burmese_text_promising_media_is_not_sent_alone(self):
        for text in (
            "ဓာတ်ပုံ ပို့ပေးလိုက်ပါတယ်ရှင်",
            "ရုပ်ပုံ ပါပါတယ်",
            "ဗီဒီယို ပို့ပေးထားပါတယ်",
            "ဗွီဒီယို ကြည့်ပေးပါရှင်",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    ReplyMessageSkill._safe_text_only_fallback(text, self.REJECTED)
                )

    def test_a_burmese_text_without_a_media_promise_may_go_alone(self):
        self.assertTrue(ReplyMessageSkill._safe_text_only_fallback(
            "ဒီပစ္စည်း ရှိပါတယ်ရှင်", self.REJECTED,
        ))


class _StubAnalyzer:
    def __init__(self, result):
        self.result = result

    async def analyze(self, event, state, messages, customer_context=None):
        return self.result.model_copy(deep=True)


class _AsksForAddressAgain:
    """A generator that forgets the address was already given."""

    reply_language = "my"

    async def generate(self, event, state, analysis, tool_result=None,
                       attachments=(), unavailable_products=(), notes=()):
        return ReplyResult(
            text="Dạ bạn vui lòng cung cấp địa chỉ nhận hàng ạ",
            should_send=True, model="stub",
        )


class _Store:
    """Knows the customer's address - verified context comes from here."""

    account_id = "acc_test"

    async def get_state(self, conversation_id):
        return None

    async def recent(self, conversation_id, limit):
        return []

    async def history(self, conversation_id, limit):
        return []

    async def context(self, conversation_id, limit):
        return {"customer_facts": {"address": "No. 12, Bogyoke Road, Yangon"}}

    async def merge_customer_facts(self, *args, **kwargs):
        return None

    async def last_outbound_text(self, conversation_id):
        return None

    async def save_state(self, state):
        return None

    async def append(self, message):
        return None


class BurmeseAddressAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_known_address_fallback_is_written_in_burmese(self):
        chain = PipelineReplyProvider(
            analyzer=_StubAnalyzer(_analysis(intent="faq", next_stage="browsing")),
            router=MessageRouter(),
            state_manager=CustomerStateManager(
                ("new", "browsing", "asking_product", "human_required"),
            ),
            reply_generator=_AsksForAddressAgain(),
            reply_validator=ReplyValidator(500),
            conversation_store=_Store(),
        )

        decision = await chain.reply(ConversationContext(
            platform="tiktok_web", conversation_key="thread-1", message_id="m1",
            message_text="ဘယ်တော့ ရောက်မလဲ", customer_name="khách",
            history=[{"id": "m1", "text": "ဘယ်တော့ ရောက်မလဲ", "direction": "INBOUND"}],
        ))

        self.assertEqual(decision.model, "verified-context-fallback")
        self.assertEqual(decision.text, address_ack("my"))
        self.assertFalse(is_wrong_language("my", decision.text))


class ReplyLanguageRouteTests(unittest.IsolatedAsyncioTestCase):
    """GET/PUT /api/accounts/{id}/scenario and /reply-language, called as
    handlers: the file is the source of truth, the live push is best-effort."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = self.directory / "config.yaml"
        shutil.copy(BASE_DIR / "configs" / "config.yaml", self.path)
        self.account = SimpleNamespace(id="acc_my", control_port=28999)
        self.control = mock.AsyncMock(return_value={})
        registry = mock.Mock(config=lambda account: load_config(self.path))
        for target, value in (
            ("registry", registry),
            ("_account_or_404", lambda account_id: self.account),
            ("_control", self.control),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _saved_language(self):
        return load_config(self.path).get("messaging.reply_language")

    def test_the_picker_lists_every_language_from_the_backend(self):
        body = main.read_scenario("acc_my")

        self.assertEqual(
            [item["code"] for item in body["reply_languages"]], list(REPLY_LANGUAGES),
        )
        self.assertIn(
            {"code": "my", "label": language_label("my")}, body["reply_languages"],
        )

    async def test_choosing_burmese_persists_and_reaches_a_running_agent(self):
        body = await main.write_reply_language("acc_my", language=" MY ")

        self.assertEqual(body["reply_language"], "my")
        # Rules that follow the language came along with it.
        self.assertEqual(body["reply_style"], default_reply_style("my"))
        self.assertTrue(body["reply_style_is_default"])
        self.assertEqual(self._saved_language(), "my")
        self.assertEqual(main.read_scenario("acc_my")["reply_language"], "my")
        self.control.assert_awaited_once_with(
            self.account, "PATCH", "/control/settings",
            {"values": {"messaging.reply_language": "my"}},
        )

    async def test_a_stopped_workspace_still_keeps_the_choice(self):
        self.control.side_effect = HTTPException(status_code=503, detail="agent down")

        body = await main.write_reply_language("acc_my", language="my")

        self.assertEqual(body["reply_language"], "my")
        self.assertEqual(self._saved_language(), "my")

    async def test_an_unset_style_shows_the_languages_default_rules(self):
        await main.write_reply_language("acc_my", language="my")
        body = main.read_scenario("acc_my")
        self.assertEqual(body["reply_style"], default_reply_style("my"))
        self.assertTrue(body["reply_style_is_default"])

    async def test_saving_the_default_rules_unchanged_keeps_following_the_language(self):
        """Otherwise opening the editor and pressing Save would freeze
        Vietnamese rules into a shop that later switches to Burmese."""
        await main.write_reply_language("acc_my", language="vi")
        body = await main.write_reply_style("acc_my", rules=default_reply_style("vi"))
        self.assertTrue(body["reply_style_is_default"])
        self.assertIsNone(load_config(self.path).get("messaging.reply_style"))

        await main.write_reply_language("acc_my", language="my")
        self.assertEqual(main.read_scenario("acc_my")["reply_style"], default_reply_style("my"))

    async def test_an_operators_own_rules_are_kept_across_a_language_switch(self):
        body = await main.write_reply_style("acc_my", rules=["Xưng mình, gọi bạn."])
        self.assertFalse(body["reply_style_is_default"])
        await main.write_reply_language("acc_my", language="my")
        self.assertEqual(main.read_scenario("acc_my")["reply_style"], ["Xưng mình, gọi bạn."])

    async def test_an_unsupported_language_is_refused_and_nothing_changes(self):
        before = self._saved_language()

        with self.assertRaises(HTTPException) as raised:
            await main.write_reply_language("acc_my", language="burmese")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self._saved_language(), before)
        self.control.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
