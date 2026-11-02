"""Opt-in live Gemini simulations for safe product-image decisions.

These tests never open TikTok or execute a send action. They stop after the
reply pipeline has either selected one catalogue-owned attachment or refused
an unsafe/underspecified media request.
"""

import os
import unittest

from dotenv import load_dotenv
from tests.support import fixture_product_root

from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from load_config import load_config
from storage.product_repository import ProductRepository
from tools.business import register_business_services
from tools.registry import BusinessToolRegistry


@unittest.skipUnless(
    os.environ.get("RUN_GEMINI_INTEGRATION") == "1",
    "set RUN_GEMINI_INTEGRATION=1 for live Gemini simulations",
)
class GeminiImageScenarios(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        load_dotenv()
        config = load_config()
        self.client = GeminiFlashLiteProvider(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model="gemini-3.1-flash-lite",
            base_url=config.get("llm.base_url"),
            timeout_seconds=60,
        )
        tools = BusinessToolRegistry(["get_product", "check_inventory"])
        products = ProductRepository(fixture_product_root())
        register_business_services(tools, product=products, inventory=products)
        stages = config.get("routing.registered_stages")
        lookup = ProductLookupPolicy(True)
        self.provider = PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                self.client,
                config.get("routing.registered_intents"),
                stages,
                tools.names,
            ),
            router=MessageRouter(),
            state_manager=CustomerStateManager(stages),
            reply_generator=ReplyGenerator(self.client, 1000, lookup),
            reply_validator=ReplyValidator(1000),
            business_tools=tools,
            product_lookup=lookup,
            enabled_media_kinds=("image", "video"),
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def _reply(self, case_id, message):
        return await self.provider.reply(ConversationContext(
            platform="tiktok_web",
            conversation_key=f"gemini-image-simulation-{case_id}",
            message_id=f"in-{case_id}",
            message_text=message,
        ))

    async def test_named_products_select_one_verified_image(self):
        cases = (
            ("milo", "Cho mình xem ảnh sữa Milo 180ml", "sp001"),
            ("ong-tho", "Gửi mình hình ảnh Sữa Ông Thọ 380g", "sp003"),
        )
        for case_id, message, product_id in cases:
            with self.subTest(case=case_id):
                decision = await self._reply(case_id, message)
                self.assertTrue(decision.should_send, decision.reason)
                self.assertEqual(decision.analysis.requested_media, ["image"])
                self.assertEqual(decision.analysis.tool_name, "get_product")
                self.assertEqual(len(decision.attachments), 1)
                attachment = decision.attachments[0]
                self.assertEqual(attachment.kind, "image")
                self.assertIn(f"data/accounts/acct-a/products/{product_id}/images/", attachment.path)
                self.assertTrue(os.path.isfile(attachment.path))

    async def test_a_product_the_shop_does_not_sell_never_gets_another_products_photo(self):
        """"sữa chua" shares the word "sữa" with every dairy item in the
        catalogue. Seen live: keyword search ranked Milo first and the agent
        sent a Milo photo for a yogurt request. The only right answer is a
        handoff with no attachment."""
        decision = await self._reply(
            "sua-chua", "Cho mình xem hình sản phẩm sữa chua 100g"
        )
        self.assertTrue(decision.requires_human, decision.reason)
        self.assertEqual(decision.attachments, ())
        for token in ("milo", "ông thọ", "venus"):
            self.assertNotIn(token, decision.text.casefold(), decision.text)

    async def test_price_question_does_not_attach_an_image(self):
        decision = await self._reply("price-only", "Milo 180ml giá bao nhiêu?")
        self.assertTrue(decision.should_send, decision.reason)
        self.assertEqual(decision.analysis.requested_media, [])
        self.assertEqual(decision.attachments, ())

    async def test_image_request_without_product_gets_a_safe_handoff_ack(self):
        """No product is named, so no image can be verified against the
        catalogue. `PipelineReplyProvider._human()` sends a fixed, fact-free
        acknowledgement and flags the conversation for a person - it does not
        stay silent, and it must never attach or claim an unverified image."""
        decision = await self._reply(
            "missing-product", "Gửi mình một ảnh sản phẩm nhé"
        )
        self.assertTrue(decision.should_send)
        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "requested_image_unavailable")
        self.assertEqual(decision.analysis.requested_media, ["image"])
        self.assertEqual(decision.attachments, ())

    async def test_customer_image_without_caption_is_acknowledged(self):
        decision = await self.provider.reply(ConversationContext(
            platform="tiktok_web",
            conversation_key="gemini-customer-media-only",
            message_id="in-media-only",
            message_text="",
            inbound_media=[{"kind": "image", "src": "blob:customer-image"}],
        ))
        self.assertTrue(decision.should_send, decision.reason)
        self.assertEqual(decision.attachments, ())
        self.assertEqual(decision.analysis.requested_media, [])
        self.assertTrue(
            "ảnh" in decision.text.casefold() or "hình" in decision.text.casefold(),
            decision.text,
        )

    async def test_known_address_is_answered_without_asking_for_it_again(self):
        decision = await self.provider.reply(ConversationContext(
            platform="tiktok_web",
            conversation_key="gemini-known-address",
            message_id="in-known-address",
            message_text=(
                "Mình tên Nguyễn Văn A, số điện thoại 0912345678, "
                "nhận hàng tại số 10 đường ABC, Hà Nội."
            ),
        ))

        self.assertTrue(decision.should_send, decision.reason)
        normalized = " ".join(decision.text.casefold().split())
        self.assertFalse(
            "địa chỉ" in normalized and any(
                phrase in normalized for phrase in (
                    "cung cấp", "cho em xin", "cho mình xin", "vui lòng cho",
                    "gửi lại", "xin địa chỉ",
                )
            ),
            decision.text,
        )


if __name__ == "__main__":
    unittest.main()
