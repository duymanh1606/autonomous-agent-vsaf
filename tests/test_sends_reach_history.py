"""Every reply that is on TikTok is in the dashboard's history, exactly once.

Two sends used to reach the customer without ever being written down: a
follow-up nudge (its own send task, outside the inbox scan that records
every other agent reply), and a reply whose Send went through but whose
place after the customer's message the thread could not prove. Both are now
stored with no DOM position, so the next re-read of the thread re-keys the
row instead of adding a second one.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tasks import Task
from platforms.tiktok.messaging import ReplyMessageSkill
from storage.conversation_context import ConversationContextRepository
from storage.repositories import TaskRepository
from tests.support import make_database


CONVERSATION = "conv-1"
NUDGE = "Dạ bạn còn quan tâm sản phẩm không ạ?"


def message(direction, text, *, order=None):
    return SimpleNamespace(direction=direction, text=text, order=order, media=[])


def followup_task(**payload):
    return Task(
        id="followup-1-conv-1-m1", account_id="acc_test",
        capability="send_message", platform="tiktok_web",
        payload={
            "conversation_id": CONVERSATION, "recipient": "customer",
            "response": NUDGE, "confirm_send": True, "origin": "agent",
            "client_message_id": "followup-1-conv-1-m1", "automatic": True,
            **payload,
        },
    )


class HistoryRowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self, account_id="acc_test")
        self.tasks = TaskRepository(self.database)
        self.messages = ConversationContextRepository(self.database)
        # Follow-ups only ever go to a conversation that already exists.
        self.messages.register_conversation(CONVERSATION)

    def _rows(self):
        return self.database.conn.execute(
            "SELECT direction, text, source FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s ORDER BY id",
            ("acc_test", CONVERSATION),
        ).fetchall()

    async def _complete(self, task, success=True):
        await self.tasks.submit(task)
        await self.tasks.complete(task.id, success, None, {}, task=task)

    async def test_a_delivered_followup_is_written_down(self):
        await self._complete(followup_task())

        rows = self._rows()
        self.assertEqual([(r["direction"], r["text"]) for r in rows], [("OUTBOUND", NUDGE)])

    async def test_a_failed_followup_is_not_written_down(self):
        await self._complete(followup_task(), success=False)

        self.assertEqual(self._rows(), [])

    async def test_a_non_automatic_agent_send_is_left_to_its_own_recording(self):
        await self._complete(followup_task(automatic=False))

        self.assertEqual(self._rows(), [])

    async def test_a_followup_that_cannot_be_stored_still_completes_its_task(self):
        task = followup_task(conversation_id="never-registered")

        await self._complete(task)

        status = self.database.conn.execute(
            "SELECT status FROM tasks WHERE account_id = %s AND id = %s",
            ("acc_test", task.id),
        ).fetchone()["status"]
        self.assertEqual(status, "completed")

    async def test_a_followup_read_back_from_the_thread_stays_one_row(self):
        await self._complete(followup_task())

        await self.messages.store_visible_messages(
            CONVERSATION, [message("OUTBOUND", NUDGE, order=3)], source="dom",
        )

        self.assertEqual(len(self._rows()), 1)

    async def test_an_unverified_reply_loses_its_mark_once_the_thread_shows_it(self):
        await self.messages.store_visible_messages(
            CONVERSATION, [message("OUTBOUND", NUDGE)], source="unverified",
        )
        self.assertEqual([r["source"] for r in self._rows()], ["unverified"])

        await self.messages.store_visible_messages(
            CONVERSATION, [message("OUTBOUND", NUDGE, order=2)], source="dom",
        )

        self.assertEqual([r["source"] for r in self._rows()], ["dom"])


class ReplyVisibleTests(unittest.IsolatedAsyncioTestCase):
    """The weak evidence `_reply_visible` reports: newest bubble is ours."""

    def _context(self, messages):
        state = SimpleNamespace(messages=messages)

        async def read_conversation():
            return state

        verifier = SimpleNamespace(read_conversation=read_conversation)
        return SimpleNamespace(platform=SimpleNamespace(state_verifier=verifier))

    async def _visible(self, messages, response="Dạ còn hàng ạ"):
        return await ReplyMessageSkill()._reply_visible(
            self._context(messages), response,
        )

    async def test_reply_as_the_newest_bubble_is_visible(self):
        # The case seen live: the thread rendered only the reply itself.
        self.assertTrue(await self._visible([
            {"id": "o1", "text": "Dạ còn hàng ạ", "direction": "OUTBOUND"},
        ]))

    async def test_reply_followed_by_a_customer_message_is_not_newest(self):
        self.assertFalse(await self._visible([
            {"id": "o1", "text": "Dạ còn hàng ạ", "direction": "OUTBOUND"},
            {"id": "i1", "text": "Bao nhiêu tiền?", "direction": "INBOUND"},
        ]))

    async def test_other_text_as_the_newest_bubble_is_not_the_reply(self):
        self.assertFalse(await self._visible([
            {"id": "o1", "text": "Dạ shop chào bạn", "direction": "OUTBOUND"},
        ]))

    async def test_a_media_only_reply_is_never_claimed(self):
        self.assertFalse(await self._visible([
            {"id": "o1", "text": "x", "direction": "OUTBOUND"},
        ], response=""))

    async def test_an_empty_thread_shows_nothing(self):
        self.assertFalse(await self._visible([]))
