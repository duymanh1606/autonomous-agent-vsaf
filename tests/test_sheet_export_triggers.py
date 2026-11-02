"""Every write that changes what a customer's sheet row should show must
trigger an export - identity from inbox scans (customer_profile) and
stage/intent from replies (customer_states) land on unrelated paths, and a
customer seen in the inbox but never answered still gets a row."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations import sheet_export
from memory.conversation_store import PostgresConversationStore
from models.customer_state import CustomerState
from storage.conversation_context import ConversationContextRepository
from tests.support import make_database


THREAD = "0:1:6981093337449300994:7278178670136558593"


class SheetExportTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.repository = ConversationContextRepository(self.database, shop_name="Shop A")
        self.store = PostgresConversationStore(self.database, shop_name="Shop A")
        self.exports: list[dict] = []

        async def capture(state, **identity):
            self.exports.append({
                "conversation_id": state.conversation_id, "stage": state.stage,
                "intent": state.intent, **identity,
            })

        patcher = mock.patch.object(sheet_export, "export_customer_state", capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _drain(self):
        # spawn_export_customer_state schedules the coroutine as a task; let it run.
        for task in list(sheet_export._background_tasks):
            await task

    async def test_inbox_scan_exports_unanswered_customer_at_stage_new(self):
        await self.repository.store_visible_messages(
            THREAD,
            [SimpleNamespace(text="cho hỏi giá", direction="INBOUND")],
            customer_id=THREAD, platform="tiktok_web",
            username="helloworld!!", display_name="helloworld!!",
        )
        await self._drain()
        self.assertEqual(len(self.exports), 1)
        self.assertEqual(self.exports[0]["conversation_id"], THREAD)
        self.assertEqual(self.exports[0]["stage"], "new")
        self.assertEqual(self.exports[0]["id_tiktok"], "helloworld!!")
        self.assertEqual(self.exports[0]["shop"], "Shop A")

    async def test_scraped_phone_exports_with_existing_stage(self):
        await self.store.save_state(CustomerState(
            customer_id=THREAD, conversation_id=THREAD, stage="asking_price", intent="hỏi giá",
        ))
        await self.repository.merge_facts(
            THREAD, {"phone": "0912345678"}, source_message_id="m1", confidence=0.95,
            username="helloworld!!",
        )
        await self._drain()
        self.assertEqual(self.exports[-1]["stage"], "asking_price")
        self.assertEqual(self.exports[-1]["phone"], "0912345678")
        self.assertEqual(self.exports[-1]["id_tiktok"], "helloworld!!")

    async def test_address_from_chat_is_exported_with_its_city(self):
        await self.repository.merge_facts(
            THREAD, {"address": "5 Hai Bà Trưng", "city": "Hà Nội"},
            source_message_id="m2", confidence=0.95, username="helloworld!!",
        )
        await self._drain()
        self.assertEqual(self.exports[-1]["chat_address"], "5 Hai Bà Trưng, Hà Nội")


if __name__ == "__main__":
    unittest.main()
