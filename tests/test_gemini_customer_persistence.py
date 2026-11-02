"""Opt-in Gemini 3.1 Flash Lite check for durable customer persistence.

The test never opens TikTok or sends a message.  Gemini analyzes one simulated
inbound customer message, then the real PostgreSQL repositories are queried to
prove identity, profile and explicit facts were stored together.
"""

import json
import os
import unittest

from dotenv import load_dotenv

from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from load_config import load_config
from models.reply import ReplyResult
from tests.support import make_database
from memory.conversation_store import PostgresConversationStore


THREAD = "gemini-customer-persistence-thread"


class SafeGenerator:
    async def generate(self, event, state, analysis, tool_result=None, attachments=(), unavailable_products=(), notes=()):
        return ReplyResult(
            text="Dạ, shop đã ghi nhận thông tin của bạn ạ.",
            should_send=True,
            model="persistence-test",
        )


@unittest.skipUnless(
    os.environ.get("RUN_GEMINI_INTEGRATION") == "1",
    "set RUN_GEMINI_INTEGRATION=1 for the live model check",
)
class GeminiCustomerPersistenceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_analysis_persists_complete_customer_identity_and_contact(self):
        load_dotenv()
        config = load_config()
        database = make_database(self)
        store = PostgresConversationStore(database)
        stages = config.get("routing.registered_stages")
        client = GeminiFlashLiteProvider(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model="gemini-3.1-flash-lite",
            base_url=config.get("llm.base_url"),
            timeout_seconds=60,
        )
        provider = PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                client,
                config.get("routing.registered_intents"),
                stages,
            ),
            router=MessageRouter(
                config.get("routing.strong_llm_threshold", 0.65),
                config.get("routing.human_intents", []),
            ),
            state_manager=CustomerStateManager(stages),
            reply_generator=SafeGenerator(),
            reply_validator=ReplyValidator(1000),
            conversation_store=store,
        )
        try:
            await provider.reply(ConversationContext(
                platform="tiktok_web",
                conversation_key=THREAD,
                message_id="gemini-contact-1",
                message_text=(
                    "Mình tên Nguyễn Văn A, số điện thoại 0912345678, "
                    "email test@example.com, nhận hàng tại số 10 đường ABC, Hà Nội."
                ),
                customer_name="gemini_customer_01",
                pending_messages=[{
                    "id": "gemini-contact-1",
                    "text": "Thông tin giao hàng và liên hệ",
                    "index": 0,
                }],
            ))
        finally:
            await client.close()

        customer = database.conn.execute(
            "SELECT * FROM account_customers "
            "WHERE account_id = %s AND customer_id = %s",
            (database.account_id, THREAD),
        ).fetchone()
        self.assertEqual(customer["username"], "gemini_customer_01")
        self.assertEqual(
            json.loads(customer["metadata"])["display_name"],
            "gemini_customer_01",
        )

        profile = database.conn.execute(
            "SELECT * FROM customer_profile "
            "WHERE account_id = %s AND conversation_id = %s",
            (database.account_id, THREAD),
        ).fetchone()
        self.assertEqual(profile["username"], "gemini_customer_01")
        self.assertEqual(profile["display_name"], "Nguyễn Văn A")
        self.assertEqual(profile["phone"], "0912345678")
        self.assertEqual(profile["email"], "test@example.com")
        self.assertIn("số 10 đường ABC", profile["address"])

        facts = {
            row["fact_type"]: row["fact_value"]
            for row in database.conn.execute(
                "SELECT fact_type, fact_value FROM customer_facts "
                "WHERE account_id = %s AND conversation_id = %s",
                (database.account_id, THREAD),
            ).fetchall()
        }
        self.assertEqual(facts["name"], "Nguyễn Văn A")
        self.assertEqual(facts["phone"], "0912345678")
        self.assertEqual(facts["email"], "test@example.com")
        self.assertIn("số 10 đường ABC", facts["address"])


if __name__ == "__main__":
    unittest.main()
