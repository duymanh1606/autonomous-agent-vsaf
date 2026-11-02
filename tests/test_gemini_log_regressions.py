"""Live Gemini simulation for media/handoff regressions observed in TikTok logs.

No browser is opened and no TikTok message is sent. Decisions run through the
real analyzer, catalogue lookup, reply generator, validator and PostgreSQL CRM.
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
from memory.conversation_store import PostgresConversationStore
from storage.product_repository import ProductRepository
from tests.support import make_database
from tools.business import register_business_services
from tools.registry import BusinessToolRegistry


@unittest.skipUnless(
    os.environ.get("RUN_GEMINI_INTEGRATION") == "1",
    "set RUN_GEMINI_INTEGRATION=1 for the live model check",
)
class GeminiObservedLogRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        load_dotenv()
        self.config = load_config()
        self.database = make_database(self)
        self.store = PostgresConversationStore(self.database)
        self.client = GeminiFlashLiteProvider(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model="gemini-3.1-flash-lite",
            base_url=self.config.get("llm.base_url"),
            timeout_seconds=60,
        )
        tools = BusinessToolRegistry(["get_product", "check_inventory"])
        products = ProductRepository(fixture_product_root())
        register_business_services(tools, product=products, inventory=products)
        stages = self.config.get("routing.registered_stages")
        lookup = ProductLookupPolicy(True)
        self.provider = PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                self.client,
                self.config.get("routing.registered_intents"),
                stages,
                tools.names,
            ),
            router=MessageRouter(
                self.config.get("routing.strong_llm_threshold", 0.65),
                self.config.get("routing.human_intents", []),
            ),
            state_manager=CustomerStateManager(stages),
            reply_generator=ReplyGenerator(self.client, 1000, lookup),
            reply_validator=ReplyValidator(1000),
            business_tools=tools,
            product_lookup=lookup,
            conversation_store=self.store,
            enabled_media_kinds=("image", "video"),
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def test_image_followups_from_the_log_produce_verified_milo_attachments(self):
        history = [
            {
                "id": "m-product",
                "direction": "INBOUND",
                "text": "Chào tôi muốn mua sữa Milo",
            },
            {
                "id": "m-product-reply",
                "direction": "OUTBOUND",
                "text": "Shop có sữa lúa mạch Milo 180ml.",
            },
        ]
        for index, message in enumerate(("Có minh hoạ không", "Đâu hình ảnh đâu")):
            with self.subTest(message=message):
                decision = await self.provider.reply(ConversationContext(
                    platform="tiktok_web",
                    conversation_key=f"gemini-media-followup-{index}",
                    message_id=f"media-{index}",
                    message_text=message,
                    customer_name="gemini_media_customer",
                    history=history + [{
                        "id": f"media-{index}",
                        "direction": "INBOUND",
                        "text": message,
                    }],
                ))

                self.assertTrue(decision.should_send, decision.reason)
                self.assertEqual(decision.analysis.requested_media, ["image"])
                self.assertEqual(len(decision.attachments), 1)
                self.assertEqual(decision.attachments[0].kind, "image")
                self.assertIn("data/accounts/acct-a/products/sp001/", decision.attachments[0].path)
                self.assertTrue(os.path.isfile(decision.attachments[0].path))

    async def test_address_change_without_a_new_value_is_not_saved_as_address(self):
        thread = "gemini-address-change-without-value"
        decision = await self.provider.reply(ConversationContext(
            platform="tiktok_web",
            conversation_key=thread,
            message_id="address-change-1",
            message_text="Tôi muốn đổi địa chỉ với sdt",
            customer_name="gemini_address_customer",
            history=[{
                "id": "address-change-1",
                "direction": "INBOUND",
                "text": "Tôi muốn đổi địa chỉ với sdt",
            }],
        ))

        self.assertTrue(decision.should_send, decision.reason)
        facts = await self.store.context_repository.get_customer_facts(thread)
        self.assertNotIn("address", facts)
        profile = (await self.store.context(thread))["customer_profile"]
        self.assertIsNone(profile["address"])
        if decision.requires_human:
            self.assertEqual(decision.model, "human-handoff-ack")
            self.assertIn("chuyển nhân viên", decision.text)


if __name__ == "__main__":
    unittest.main()
