"""Conversations read on another device still reach the dashboard.

Staff read customers' messages in the TikTok phone app. That clears TikTok's
unread marker, so the scan - which only opens unread rows - never opened those
conversations and their messages never reached the database. On a scan with
nothing unread, the newest rows' previews are compared with the last sync and
one changed conversation is opened and copied. It is never answered: a person
on that device already saw it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_service import ReplyDecision
from models.result import ActionResult
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.screens import TikTokScreen
from tests.test_inbox_scan_flow import StubContext, observation_for, scan_task


THREAD = [
    {"id": "m1", "text": "Shop ơi còn size M không", "direction": "INBOUND"},
    {"id": "m2", "text": "Dạ còn ạ", "direction": "OUTBOUND"},
    {"id": "m3", "text": "Ship về Hà Nội bao lâu", "direction": "INBOUND"},
]


class RecordingProvider:
    def __init__(self):
        self.persisted = []
        self.replied = []

    async def persist_thread(self, context):
        self.persisted.append(context)

    async def reply(self, context):
        self.replied.append(context)
        return ReplyDecision(text="x", should_send=True, reason="generated", model="stub")

    async def record_sent(self, context, text, attachments=()):
        pass


def row(name, preview, *, unread=False, request=False):
    return {"name": name, "preview": preview, "unread": unread, "message_request": request}


class SyncReadElsewhereTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = RecordingProvider()
        self.skill = InboxScanSkill(reply_provider=self.provider, sync_cooldown_seconds=0)

    def context(self, rows, *, unread=(), thread=THREAD, screen=TikTokScreen.MESSAGES):
        opened = observation_for(TikTokScreen.CONVERSATION, messages=thread, active="")
        return StubContext(observation_for(screen), {
            "platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": list(unread)}}),
            "platform.list_conversations": ActionResult(
                success=True, data={"tool": {"rows": list(rows)}}),
            "platform.open_conversation": (
                ActionResult(success=True, data={"tool": {"success": True}}), opened),
            "platform.open_inbox": ActionResult(success=True, data={}),
        })

    def opened(self, context):
        return context.actions.count("platform.open_conversation")

    async def test_a_conversation_read_on_the_phone_is_copied_not_answered(self):
        context = self.context([row("lan", "Ship về Hà Nội bao lâu · 2m")])
        result = await self.skill.execute(scan_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(self.opened(context), 1)
        [persisted] = self.provider.persisted
        self.assertEqual(len(persisted.history), 3)
        self.assertEqual(self.provider.replied, [])
        statuses = [item["status"] for item in result.data["conversations_handled"]]
        self.assertEqual(statuses, ["synced_read_elsewhere"])

    async def test_an_unchanged_preview_is_not_opened_again(self):
        rows = [row("lan", "Ship về Hà Nội bao lâu · 2m")]
        await self.skill.execute(scan_task(), self.context(rows))
        # Only the relative time moved.
        context = self.context([row("lan", "Ship về Hà Nội bao lâu · 3m")])
        await self.skill.execute(scan_task(), context)
        self.assertEqual(self.opened(context), 0)

    async def test_a_changed_preview_is_synced_again(self):
        await self.skill.execute(scan_task(), self.context([row("lan", "Ship về Hà Nội bao lâu")]))
        context = self.context([row("lan", "Ok shop, mình chốt 2 cái")])
        await self.skill.execute(scan_task(), context)
        self.assertEqual(self.opened(context), 1)

    async def test_one_conversation_per_scan_newest_first(self):
        context = self.context([row("lan", "a"), row("minh", "b")])
        await self.skill.execute(scan_task(), context)
        self.assertEqual(self.opened(context), 1)
        self.assertEqual(self.provider.persisted[0].customer_name, "lan")

        context = self.context([row("lan", "a"), row("minh", "b")])
        await self.skill.execute(scan_task(), context)
        self.assertEqual(self.provider.persisted[1].customer_name, "minh")

    async def test_the_cooldown_limits_how_often_one_conversation_is_reopened(self):
        skill = InboxScanSkill(reply_provider=self.provider, sync_cooldown_seconds=300)
        await skill.execute(scan_task(), self.context([row("lan", "a")]))
        context = self.context([row("lan", "b")])
        await skill.execute(scan_task(), context)
        self.assertEqual(self.opened(context), 0)

    async def test_unread_rows_and_requests_are_left_to_the_scan(self):
        context = self.context([row("lan", "a", unread=True), row("req", "b", request=True)])
        await self.skill.execute(scan_task(), context)
        self.assertIn("platform.list_conversations", context.actions)
        self.assertEqual(self.opened(context), 0)

    async def test_a_conversation_the_ledger_still_owes_is_left_to_the_scan(self):
        """The scan reopens it itself (a revisit) and may answer it."""
        self.skill._unanswered["minh"] = {"name": "minh", "attempts": 1}
        context = self.context([row("minh", "c")])
        self.assertEqual(await self.skill._sync_read_elsewhere(scan_task(), context), [])
        self.assertEqual(self.opened(context), 0)

    async def test_no_sync_while_there_is_unread_work(self):
        context = self.context([row("lan", "a")], unread=["khach"])
        context.results["platform.open_unread_conversation"] = (
            ActionResult(success=True, data={"tool": {"success": True}}),
            observation_for(TikTokScreen.CONVERSATION, messages=[
                {"id": "x1", "text": "hi", "direction": "OUTBOUND"},
            ]),
        )
        await self.skill.execute(scan_task(), context)
        self.assertNotIn("platform.list_conversations", context.actions)

    async def test_the_next_scan_does_not_answer_the_synced_thread(self):
        """The synced thread stays open, and every scan starts by reading the
        open thread - its customer message must already count as handled."""
        self.skill.reply_skill = object()
        await self.skill.execute(scan_task(), self.context([row("lan", "a")]))

        context = self.context([row("lan", "a")], screen=TikTokScreen.CONVERSATION)
        context.observation = observation_for(
            TikTokScreen.CONVERSATION, messages=THREAD, active="lan",
        )
        result = await self.skill.execute(scan_task(), context)
        self.assertEqual(self.provider.replied, [])
        self.assertEqual(result.data["active_conversation"]["status"], "already_handled")

    async def test_switched_off(self):
        skill = InboxScanSkill(reply_provider=self.provider, sync_read_elsewhere=False)
        context = self.context([row("lan", "a")])
        await skill.execute(scan_task(), context)
        self.assertNotIn("platform.list_conversations", context.actions)

    def test_the_preview_signature_ignores_times_and_dates(self):
        signature = InboxScanSkill._preview_signature
        self.assertEqual(signature("Còn hàng không · 2h"), signature("Còn hàng không · 5h"))
        self.assertEqual(signature("Còn hàng không 14:05"), signature("Còn hàng không 09:30"))
        self.assertEqual(signature("Ok · Hôm qua"), signature("Ok · 3/10"))
        self.assertNotEqual(signature("Còn hàng không"), signature("Hết hàng rồi"))


class SyncPassesTheRealPolicyTests(unittest.IsolatedAsyncioTestCase):
    """The tests above use a stub context, which never consults the policy
    gate - so they passed while production refused the scan's listing call
    (it was allowed for send tasks only) and the sync silently did nothing."""

    def policy_allows(self, capability):
        from core.policy import PolicyEngine
        from models.action import Action
        from models.auth import AuthState
        from models.tasks import Task
        from tests.test_policy_conversation_control import build_context

        policy = PolicyEngine(allowed_tools={"platform.list_conversations",
                                             "platform.open_conversation"})
        context = build_context()
        context.current_screen = "TIKTOK_MESSAGES"
        context.auth_state = AuthState.LOGGED_IN
        task = Task(account_id="acc_test", id="t1", capability=capability,
                    platform="tiktok_web")
        return policy.allow(Action(name="platform.list_conversations", args={"limit": 10},
                                   reason="test"), context, task)

    async def test_an_inbox_scan_may_list_conversations(self):
        self.assertTrue(await self.policy_allows("messaging.scan_inbox"))

    async def test_an_unrelated_task_still_may_not(self):
        self.assertFalse(await self.policy_allows("open_profile"))


if __name__ == "__main__":
    unittest.main()
