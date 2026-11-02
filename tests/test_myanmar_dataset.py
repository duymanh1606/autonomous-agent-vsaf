"""The Burmese customer-care dataset through the deterministic guards.

Credential-free: no LLM. What the model would answer is measured separately
by tests/gemini_myanmar_dataset_evaluation.py; this pins what code decides on
its own for a `reply_language: my` shop - the script check, the media-request
reader and the price backstop - on real Burmese phrasings rather than ones
written for the test.

Skipped when the workbook is absent (data/ is gitignored).
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_analyzer import MessageAnalyzer
from agent.reply_language import is_wrong_language
from agent.reply_validator import ReplyValidator
from models.analysis import MessageAnalysis
from models.reply import ReplyResult
from models.tool import ToolResult
from tests.myanmar_dataset import dataset_path, load_cases

CASES = load_cases()

_MYANMAR_DIGITS = str.maketrans({chr(0x1040 + value): str(value) for value in range(10)})


def _analysis(**overrides):
    data = {"intent": "faq", "next_stage": "browsing", "confidence": 0.9}
    data.update(overrides)
    return MessageAnalysis(**data)


def _has_burmese_letters(text: str) -> bool:
    return any("က" <= char <= "႟" and char.isalpha() for char in text)


def _kyat(reply: str) -> int:
    return int(re.search(r"(\d+)\s*ကျပ်", reply.translate(_MYANMAR_DIGITS)).group(1))


@unittest.skipUnless(CASES, f"Myanmar dataset not found ({dataset_path()})")
class MyanmarDatasetShapeTests(unittest.TestCase):
    def test_the_workbook_is_what_these_tests_assume(self):
        self.assertEqual(len(CASES), len({case.test_id for case in CASES}))
        self.assertTrue(all(case.customer_message for case in CASES))
        self.assertTrue(all(case.expected_reply_mm for case in CASES))


@unittest.skipUnless(CASES, f"Myanmar dataset not found ({dataset_path()})")
class MyanmarDatasetScriptTests(unittest.TestCase):
    def test_every_burmese_reference_reply_passes_the_burmese_check(self):
        for case in CASES:
            with self.subTest(test_id=case.test_id):
                self.assertFalse(is_wrong_language("my", case.expected_reply_mm))

    def test_every_vietnamese_reference_reply_is_wrong_for_a_myanmar_shop(self):
        # Some Vietnamese references keep a Burmese product name from the
        # template ("Giá ... ထုတ်ကုန် A là ၂၀၀၀၀ kyat ạ"); with a long enough
        # name most of the non-Latin letters are Burmese and the line is
        # genuinely ambiguous, so only those without Burmese letters are
        # judged here. Myanmar digits stay in: they prove nothing.
        for case in CASES:
            if _has_burmese_letters(case.expected_reply_vi):
                continue
            with self.subTest(test_id=case.test_id):
                self.assertTrue(is_wrong_language("my", case.expected_reply_vi))

    def test_a_myanmar_digit_does_not_make_vietnamese_burmese(self):
        """The Myanmar check used to accept any character from the block,
        digits included, so a Vietnamese delivery estimate with one Myanmar
        numeral was never retried for a `my` shop."""
        candidates = {
            case.expected_reply_vi for case in CASES
            if case.intent == "shipping"
            and re.fullmatch(r"[^က-ဿ]*[၀-၉]+[^က-ဿ]*", case.expected_reply_vi)
        }
        self.assertTrue(candidates)
        accepted = sorted({
            case.expected_reply_vi for case in CASES
            if case.intent == "shipping"
            and re.fullmatch(r"[^က-ဿ]*[၀-၉]+[^က-ဿ]*", case.expected_reply_vi)
            and not is_wrong_language("my", case.expected_reply_vi)
        })
        self.assertEqual(accepted, [])

    def test_every_vietnamese_reference_reply_passes_the_vietnamese_check(self):
        # Same exclusion as above, from the other side: a Vietnamese line
        # whose long Burmese product name outweighs its own accented letters
        # is ambiguous, and a retry is all it would cost.
        for case in CASES:
            if _has_burmese_letters(case.expected_reply_vi):
                continue
            with self.subTest(test_id=case.test_id):
                self.assertFalse(is_wrong_language("vi", case.expected_reply_vi))

    def test_a_burmese_reply_is_wrong_for_a_vietnamese_shop(self):
        wrongly_accepted = [
            case.test_id for case in CASES
            if not is_wrong_language("vi", case.expected_reply_mm)
        ]
        self.assertEqual(wrongly_accepted, [])


@unittest.skipUnless(CASES, f"Myanmar dataset not found ({dataset_path()})")
class MyanmarDatasetMediaReaderTests(unittest.TestCase):
    """No message in the dataset asks for a photo or video, so the reader
    must not hand any of them catalogue media."""

    def test_no_customer_message_reads_as_a_media_request(self):
        # Includes "အသုံးပြုပုံ သိချင်ပါတယ်" (how to use it), once read as a
        # photo request because -ပုံ is also the "way of" suffix.
        for message in sorted({case.customer_message for case in CASES}):
            with self.subTest(message=message):
                self.assertEqual(MessageAnalyzer._explicit_media_requests(message), [])


@unittest.skipUnless(CASES, f"Myanmar dataset not found ({dataset_path()})")
class MyanmarDatasetPriceBackstopTests(unittest.TestCase):
    """Every reference price reply names an amount in Myanmar digits and kyat.
    The validator must read each one: refused with no product data, allowed
    when the product data states that amount."""

    PRICE_REPLIES = sorted({case.expected_reply_mm for case in CASES if case.intent == "price"})

    def test_the_dataset_has_price_replies(self):
        self.assertTrue(self.PRICE_REPLIES)

    def test_a_kyat_price_without_product_data_is_refused(self):
        for reply in self.PRICE_REPLIES:
            with self.subTest(reply=reply):
                result = ReplyValidator(500).validate(
                    ReplyResult(text=reply, should_send=True),
                    _analysis(intent="ask_price", next_stage="asking_price"),
                    None, None, attachments=(),
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, "unverified_price_claim")

    def test_a_kyat_price_the_product_states_is_allowed(self):
        for reply in self.PRICE_REPLIES:
            with self.subTest(reply=reply):
                result = ReplyValidator(500).validate(
                    ReplyResult(text=reply, should_send=True),
                    _analysis(
                        intent="ask_price", next_stage="asking_price",
                        requires_tool=True, tool_name="get_product",
                    ),
                    ToolResult(
                        tool_name="get_product", success=True,
                        data={"id": "sp1", "price": _kyat(reply), "stock": 3},
                    ),
                    None, attachments=(),
                )
                self.assertTrue(result.allowed, result.reason)

    def test_a_kyat_price_other_than_the_products_is_refused(self):
        for reply in self.PRICE_REPLIES:
            with self.subTest(reply=reply):
                result = ReplyValidator(500).validate(
                    ReplyResult(text=reply, should_send=True),
                    _analysis(
                        intent="ask_price", next_stage="asking_price",
                        requires_tool=True, tool_name="get_product",
                    ),
                    ToolResult(
                        tool_name="get_product", success=True,
                        data={"id": "sp1", "price": _kyat(reply) + 1000, "stock": 3},
                    ),
                    None, attachments=(),
                )
                self.assertFalse(result.allowed)


@unittest.skipUnless(CASES, f"Myanmar dataset not found ({dataset_path()})")
class MyanmarDatasetOtherRepliesTests(unittest.TestCase):
    def test_reference_replies_without_a_price_pass_the_validator(self):
        """The validator's other checks (paths, media claims, address
        re-asks, length) must not refuse ordinary Burmese replies."""
        for reply in sorted({case.expected_reply_mm for case in CASES if case.intent != "price"}):
            with self.subTest(reply=reply):
                result = ReplyValidator(500).validate(
                    ReplyResult(text=reply, should_send=True),
                    _analysis(), None, None, attachments=(),
                )
                self.assertTrue(result.allowed, result.reason)


if __name__ == "__main__":
    unittest.main()
