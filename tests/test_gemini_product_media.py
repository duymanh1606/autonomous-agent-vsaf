"""Opt-in live Gemini check for product-photo routing (never sends TikTok UI)."""

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
    "set RUN_GEMINI_INTEGRATION=1 for the live model check",
)
class GeminiProductMediaIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_milo_media_requests_return_verified_attachments(self):
        load_dotenv()
        config = load_config()
        client = GeminiFlashLiteProvider(
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
        provider = PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                client, config.get("routing.registered_intents"), stages, tools.names
            ),
            router=MessageRouter(),
            state_manager=CustomerStateManager(stages),
            reply_generator=ReplyGenerator(client, 1000, lookup),
            reply_validator=ReplyValidator(1000),
            business_tools=tools,
            product_lookup=lookup,
            enabled_media_kinds=("image", "video"),
        )
        try:
            decisions = []
            for index, (kind, message) in enumerate((
                ("image", "Cho mình xem ảnh sữa Milo 180ml với shop ơi"),
                ("video", "Shop gửi mình video sữa Milo 180ml nhé"),
            )):
                decision = await provider.reply(ConversationContext(
                    platform="tiktok_web",
                    conversation_key=f"gemini-media-check-{kind}",
                    message_id=f"in-{index}",
                    message_text=message,
                ))
                decisions.append((kind, decision))
        finally:
            await client.close()

        for kind, decision in decisions:
            with self.subTest(kind=kind):
                self.assertTrue(decision.should_send, decision.reason)
                self.assertEqual(decision.analysis.requested_media, [kind])
                self.assertEqual(decision.analysis.tool_name, "get_product")
                self.assertEqual(set(decision.analysis.tool_args), {"product_id"})
                self.assertIn("milo", decision.analysis.tool_args["product_id"].casefold())
                self.assertEqual(len(decision.attachments), 1)
                attachment = decision.attachments[0]
                self.assertEqual(attachment.kind, kind)
                self.assertTrue(os.path.isfile(attachment.path))
                self.assertIn("data/accounts/acct-a/products/sp001/", attachment.path)

    async def test_sp006_image_and_video_request_returns_both_attachments(self):
        load_dotenv()
        config = load_config()
        client = GeminiFlashLiteProvider(
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
        provider = PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                client, config.get("routing.registered_intents"), stages, tools.names
            ),
            router=MessageRouter(),
            state_manager=CustomerStateManager(stages),
            reply_generator=ReplyGenerator(client, 1000, lookup),
            reply_validator=ReplyValidator(1000),
            business_tools=tools,
            product_lookup=lookup,
            enabled_media_kinds=("image", "video"),
        )
        try:
            decision = await provider.reply(ConversationContext(
                platform="tiktok_web",
                conversation_key="gemini-sp006-both-media",
                message_id="in-sp006-both",
                message_text="Shop gửi mình cả ảnh và video Venus BigOne nhé",
            ))
        finally:
            await client.close()

        self.assertTrue(decision.should_send, decision.reason)
        self.assertEqual(decision.analysis.requested_media, ["image", "video"])
        self.assertEqual([item.kind for item in decision.attachments], ["image", "video"])
        self.assertTrue(all(os.path.isfile(item.path) for item in decision.attachments))
        self.assertTrue(all("data/accounts/acct-a/products/sp006/" in item.path for item in decision.attachments))


if __name__ == "__main__":
    unittest.main()
