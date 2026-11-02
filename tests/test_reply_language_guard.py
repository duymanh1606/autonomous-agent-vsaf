"""Reproduces a real production defect: Gemini answered a Vietnamese
customer in generic English ("Hello! Thank you for reaching out...").
`ReplyGenerator` must catch this and force one retry.

The language is the operator's choice (`messaging.reply_language`), not the
customer's: a Myanmar shop answers in Burmese even when the customer writes
Vietnamese, so the guard checks the draft against the configured language.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_generator import ReplyGenerator
from agent.reply_language import (
    DEFAULT_REPLY_LANGUAGE,
    REPLY_LANGUAGES,
    address_ack,
    handoff_ack,
    is_wrong_language,
    language_instruction,
    normalize_language,
    request_ack,
)
from agent.reply_validator import ReplyValidator
from platforms.tiktok.inbox_watcher import InboxScanSkill
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult


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


class LanguageHelperTests(unittest.TestCase):
    def test_vietnamese_is_detected_via_decomposition(self):
        self.assertFalse(is_wrong_language("vi", "Tôi muốn mua giày bóng đá"))
        self.assertTrue(is_wrong_language("vi", "Hello, how can I help you today"))

    def test_burmese_is_detected_by_its_script(self):
        self.assertFalse(is_wrong_language("my", "မင်္ဂလာပါ ဘာကူညီပေးရမလဲ"))
        self.assertTrue(is_wrong_language("my", "Hello, how can I help you today"))
        # Vietnamese is just as wrong as English for a Myanmar shop.
        self.assertTrue(is_wrong_language("my", "Dạ shop có nhiều loại giày đá bóng ạ."))

    def test_a_short_or_neutral_reply_is_never_flagged(self):
        for code in ("vi", "my"):
            self.assertFalse(is_wrong_language(code, "OK"))
            self.assertFalse(is_wrong_language(code, "259.000"))
            self.assertFalse(is_wrong_language(code, ""))

    def test_an_unknown_code_falls_back_to_the_default(self):
        self.assertEqual(normalize_language("klingon"), DEFAULT_REPLY_LANGUAGE)
        self.assertEqual(normalize_language(None), DEFAULT_REPLY_LANGUAGE)
        self.assertEqual(normalize_language(" MY "), "my")

    def test_the_instruction_names_the_language_and_forbids_switching(self):
        self.assertIn("Burmese", language_instruction("my"))
        self.assertIn("never switch to the customer's language", language_instruction("my"))
        self.assertIn("Vietnamese", language_instruction("vi"))


class CannedReplyTests(unittest.TestCase):
    """The handoff and address acknowledgements are the only replies no model
    writes - they are sent precisely when a generated one is not trusted - so a
    Burmese shop kept sending those two in Vietnamese."""

    def test_every_language_carries_both_canned_replies_in_its_own_script(self):
        for code, language in REPLY_LANGUAGES.items():
            for name, text in (
                ("handoff_ack", handoff_ack(code)),
                ("handoff_ack_with_contact", handoff_ack(code, ask_contact=True)),
                ("address_ack", address_ack(code)),
            ):
                with self.subTest(code=code, text=name):
                    self.assertTrue(text.strip())
                    self.assertTrue(language.has_script(text))
                    self.assertFalse(is_wrong_language(code, text))

    def test_an_unknown_code_still_acknowledges_in_the_default_language(self):
        self.assertEqual(handoff_ack("xx"), handoff_ack(DEFAULT_REPLY_LANGUAGE))
        self.assertEqual(address_ack(None), address_ack(DEFAULT_REPLY_LANGUAGE))


class ReplyGeneratorRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_english_reply_to_a_vietnamese_customer_is_retried_in_vietnamese(self):
        llm = ScriptedLLM([
            ReplyResult(
                text="Hello! Thank you for reaching out. How can I help you today?",
                should_send=True,
            ),
            ReplyResult(text="Dạ shop có nhiều loại giày đá bóng ạ.", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500)

        result = await generator.generate(
            _event("Tôi muốn mua giày bóng đá"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )

        self.assertEqual(len(llm.prompts), 2)
        self.assertEqual(result.text, "Dạ shop có nhiều loại giày đá bóng ạ.")
        self.assertIn("not in Vietnamese", llm.prompts[1])
        self.assertIn("Write the reply in Vietnamese", llm.prompts[0])

    async def test_a_correct_vietnamese_reply_is_never_retried(self):
        llm = ScriptedLLM([
            ReplyResult(text="Dạ shop có nhiều loại giày đá bóng ạ.", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500)

        result = await generator.generate(
            _event("Tôi muốn mua giày bóng đá"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )

        self.assertEqual(len(llm.prompts), 1)
        self.assertEqual(result.text, "Dạ shop có nhiều loại giày đá bóng ạ.")

    async def test_retry_that_is_still_wrong_keeps_the_second_attempt(self):
        """Never send the first (known-wrong) draft even if the retry also
        fails the check - the retry is the model's best remaining attempt."""
        llm = ScriptedLLM([
            ReplyResult(text="Hello! How can I help you today?", should_send=True),
            ReplyResult(text="Hi there, glad to help!", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500)

        result = await generator.generate(
            _event("Tôi muốn mua giày bóng đá"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )

        self.assertEqual(len(llm.prompts), 2)
        self.assertEqual(result.text, "Hi there, glad to help!")


class MyanmarReplyLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_vietnamese_customer_is_answered_in_burmese(self):
        """The shop's language wins over the customer's: the Vietnamese draft
        is the known-wrong one here, and the retry asks for Burmese."""
        llm = ScriptedLLM([
            ReplyResult(text="Dạ shop có nhiều loại giày đá bóng ạ.", should_send=True),
            ReplyResult(text="ဘောလုံးဖိနပ် အမျိုးအစားများစွာ ရှိပါတယ်ရှင်", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500, reply_language="my")

        result = await generator.generate(
            _event("Tôi muốn mua giày bóng đá"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )

        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("Write the reply in Burmese (Myanmar)", llm.prompts[0])
        self.assertIn("not in Burmese (Myanmar)", llm.prompts[1])
        self.assertEqual(result.text, "ဘောလုံးဖိနပ် အမျိုးအစားများစွာ ရှိပါတယ်ရှင်")

    async def test_a_correct_burmese_reply_is_never_retried(self):
        llm = ScriptedLLM([
            ReplyResult(text="ဘောလုံးဖိနပ် အမျိုးအစားများစွာ ရှိပါတယ်ရှင်", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500, reply_language="my")

        await generator.generate(
            _event("Hello, do you sell football shoes?"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )

        self.assertEqual(len(llm.prompts), 1)

    async def test_an_unknown_configured_language_still_answers(self):
        """A config typo must not stop the shop replying - it falls back to
        the default language instead of raising."""
        llm = ScriptedLLM([
            ReplyResult(text="Dạ shop có nhiều loại giày đá bóng ạ.", should_send=True),
        ])
        generator = ReplyGenerator(llm, 500, reply_language="klingon")

        self.assertEqual(generator.reply_language, DEFAULT_REPLY_LANGUAGE)
        await generator.generate(
            _event("Tôi muốn mua giày bóng đá"),
            CustomerState(customer_id="u1", conversation_id="c1"),
            _analysis(),
        )
        self.assertEqual(len(llm.prompts), 1)


class OnlyTheShopLanguageIsSentTests(unittest.TestCase):
    """The prompt and the one forced rewrite are best effort; these are the
    gates that make "a shop speaks only its configured language" hold."""

    def _validate(self, text, language):
        return ReplyValidator(500).validate(
            ReplyResult(text=text, should_send=True), _analysis(), None, None,
            reply_language=language,
        )

    def test_a_reply_still_in_the_wrong_language_is_refused(self):
        refused = self._validate("Dạ shop có nhiều loại giày đá bóng ạ.", "my")
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.reason, "wrong_reply_language")
        self.assertFalse(self._validate("Hello, how can I help you today", "vi").allowed)

    def test_a_reply_in_the_shop_language_passes(self):
        self.assertTrue(self._validate("ဘောလုံးဖိနပ် ရှိပါတယ်ရှင်", "my").allowed)
        self.assertTrue(self._validate("Dạ shop còn hàng ạ.", "vi").allowed)

    def test_the_canned_replies_pass_their_own_language(self):
        for code in REPLY_LANGUAGES:
            for text in (
                handoff_ack(code), handoff_ack(code, ask_contact=True),
                address_ack(code), request_ack(code),
            ):
                with self.subTest(code=code, text=text):
                    self.assertTrue(self._validate(text, code).allowed)

    def test_the_request_ack_follows_the_shop_language(self):
        vietnamese = "Dạ shop đã nhận được tin nhắn, mình kiểm tra và phản hồi ngay ạ."
        skill = InboxScanSkill(message_request_retry_prompt=vietnamese)
        skill.reply_provider = type("Provider", (), {"reply_language": "my"})()
        self.assertEqual(skill._request_ack_text(), request_ack("my"))
        skill.reply_provider.reply_language = "vi"
        self.assertEqual(skill._request_ack_text(), vietnamese)


if __name__ == "__main__":
    unittest.main()
