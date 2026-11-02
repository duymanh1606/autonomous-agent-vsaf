"""The shop never tells a customer it cannot send pictures.

Reported by testing: a customer asked to see a product; the reply said
"hiện tại hệ thống chưa hỗ trợ gửi hình ảnh trực tiếp qua tin nhắn ạ" and
the product photo arrived right under it. The attachments are chosen before
the reply is written, but the model was never told whether one was going
out - only a list of things not to say about media - and it filled the gap
with a refusal. Three layers now, each tested here:

* the prompt says exactly what is sent with this reply;
* a draft that denies sending media is rewritten once;
* the validator refuses any reply that still says it.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_validator import ReplyValidator, denies_media_capability
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult


REPORTED = (
    "Dạ, hiện tại hệ thống chưa hỗ trợ gửi hình ảnh trực tiếp qua tin nhắn ạ. "
    "Bộ công sở bên em có thiết kế thanh lịch và rất dễ phối đồ."
)
PHOTO = SimpleNamespace(kind="image", path="/catalogue/ao/images/a.jpg", name="a.jpg")


def _analysis():
    return MessageAnalysis(
        intent="ask_product", next_stage="asking_product",
        requires_tool=False, confidence=0.9,
    )


def _event():
    return MessageEvent(
        account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
        message_id="m", message="Cho mình xem ảnh bộ này với",
    )


class ScriptedLLM:
    """Answers with each of `replies` in turn, recording every prompt."""

    model_name = "test-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    async def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return ReplyResult(text=self.replies.pop(0), should_send=True)


class DetectionTests(unittest.TestCase):
    def test_the_reported_reply_and_its_variants_are_caught(self):
        for text in (
            REPORTED,
            "Shop không thể gửi ảnh qua đây ạ",
            "Dạ shop không gửi được video ạ",
            "Việc gửi hình ảnh qua tin nhắn hiện chưa được hỗ trợ ạ",
            "Da, he thong chua ho tro gui hinh anh a",
            "Sorry, we cannot send images here",
        ):
            with self.subTest(text=text):
                self.assertTrue(denies_media_capability(text))

    def test_ordinary_talk_about_pictures_is_left_alone(self):
        for text in (
            "Bạn chưa gửi ảnh cho shop ạ",
            "Shop gửi bạn ảnh mẫu nhé",
            "Bạn gửi giúp shop ảnh sản phẩm bị lỗi nhé",
            "Dạ sản phẩm này không có video ạ",
            "Bạn không cần gửi ảnh đâu ạ",
        ):
            with self.subTest(text=text):
                self.assertFalse(denies_media_capability(text))


class ValidatorTests(unittest.TestCase):
    def test_refused_with_or_without_an_attachment(self):
        for attachments in ((PHOTO,), ()):
            with self.subTest(attachments=bool(attachments)):
                result = ReplyValidator().validate(
                    ReplyResult(text=REPORTED, should_send=True), _analysis(),
                    tool_result=None, last_outbound_text=None, attachments=attachments,
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, "denies_media_capability")


class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    def generator(self, llm):
        return ReplyGenerator(llm, 500, ProductLookupPolicy(True))

    async def test_the_prompt_says_a_photo_goes_with_this_reply(self):
        llm = ScriptedLLM("Shop gửi bạn ảnh mẫu ạ")
        await self.generator(llm).generate(
            _event(), CustomerState(conversation_id="c", customer_id="c"), _analysis(),
            attachments=(PHOTO,),
        )
        self.assertIn("sent together with a product photo", llm.prompts[0])
        self.assertIn("Never say the shop cannot send images or videos", llm.prompts[0])

    async def test_the_prompt_says_nothing_is_attached_when_nothing_is(self):
        llm = ScriptedLLM("Dạ bộ này còn size M ạ")
        await self.generator(llm).generate(
            _event(), CustomerState(conversation_id="c", customer_id="c"), _analysis(),
        )
        self.assertIn("No image or video is sent with this reply", llm.prompts[0])
        self.assertIn("Never say the shop or the system cannot send images", llm.prompts[0])

    async def test_a_draft_that_denies_sending_pictures_is_rewritten_once(self):
        llm = ScriptedLLM(REPORTED, "Shop gửi bạn ảnh bộ công sở ạ")
        result = await self.generator(llm).generate(
            _event(), CustomerState(conversation_id="c", customer_id="c"), _analysis(),
            attachments=(PHOTO,),
        )
        self.assertEqual(result.text, "Shop gửi bạn ảnh bộ công sở ạ")
        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("That is false", llm.prompts[1])


if __name__ == "__main__":
    unittest.main()
