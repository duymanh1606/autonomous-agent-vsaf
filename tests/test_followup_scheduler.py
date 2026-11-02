"""FollowUpPolicy validation and FollowUpScheduler's silence-detection query."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.followup_policy import FollowUpPolicy
from messaging.followup_scheduler import FollowUpScheduler
from storage.repositories import TaskRepository
from tests.support import make_database


class FollowUpPolicyTests(unittest.TestCase):
    def test_a_rule_missing_a_message_is_dropped(self):
        policy = FollowUpPolicy([{"delay_minutes": 60, "message": "   "}])
        self.assertEqual(policy.rules, [])

    def test_a_rule_with_a_non_numeric_delay_is_dropped(self):
        policy = FollowUpPolicy([{"delay_minutes": "soon", "message": "hi"}])
        self.assertEqual(policy.rules, [])

    def test_delay_is_clamped_to_the_configured_range(self):
        policy = FollowUpPolicy([{"delay_minutes": 0, "message": "hi"}])
        self.assertEqual(policy.rules[0]["delay_minutes"], 1)

        policy = FollowUpPolicy([{"delay_minutes": 10**9, "message": "hi"}])
        self.assertEqual(policy.rules[0]["delay_minutes"], 60 * 24 * 14)

    def test_message_is_clipped_to_the_character_cap(self):
        policy = FollowUpPolicy([{"delay_minutes": 5, "message": "x" * 5000}])
        self.assertEqual(len(policy.rules[0]["message"]), 1000)

    def test_rules_are_capped_in_count(self):
        policy = FollowUpPolicy([
            {"delay_minutes": 5, "message": str(i)} for i in range(30)
        ])
        self.assertEqual(len(policy.rules), 20)

    def test_missing_id_is_generated_and_stable_across_a_re_normalize(self):
        policy = FollowUpPolicy([{"delay_minutes": 5, "message": "hi"}])
        rule_id = policy.rules[0]["id"]
        self.assertTrue(rule_id)
        # Editing in place (what the dashboard sends back) must not mint a
        # new id every save - a changing id would make the scheduler treat an
        # edited rule as brand new and re-fire it immediately.
        policy.set(policy.rules)
        self.assertEqual(policy.rules[0]["id"], rule_id)

    def test_enabled_rules_excludes_disabled_ones(self):
        policy = FollowUpPolicy([
            {"delay_minutes": 5, "message": "on", "enabled": True},
            {"delay_minutes": 5, "message": "off", "enabled": False},
        ])
        self.assertEqual(len(policy.enabled_rules()), 1)
        self.assertEqual(policy.enabled_rules()[0]["message"], "on")


def _register_conversation(database, conversation_id):
    database.conn.execute(
        "INSERT INTO conversations(account_id, platform_conversation_id) VALUES (%s, %s)",
        (database.account_id, conversation_id),
    )
    database.conn.commit()


def _insert_message(database, conversation_id, direction, text, created_at, message_id):
    database.conn.execute(
        "INSERT INTO conversation_messages(account_id, message_id, conversation_id, "
        "customer_id, direction, text, created_at, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'test')",
        (
            database.account_id, message_id, conversation_id, conversation_id,
            direction, text, created_at,
        ),
    )
    database.conn.commit()


class FollowUpSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.repository = TaskRepository(self.database)
        self.scheduler = FollowUpScheduler(
            self.database, self.repository, FollowUpPolicy(), platform="tiktok_web",
        )

    def _tasks(self):
        return self.database.conn.execute(
            "SELECT id, payload, priority FROM tasks WHERE account_id = %s",
            (self.database.account_id,),
        ).fetchall()

    async def test_a_silent_conversation_past_its_delay_is_queued(self):
        _register_conversation(self.database, "conv-1")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-1", "INBOUND", "Còn hàng không shop?",
                         now - timedelta(hours=3), "in-1")
        _insert_message(self.database, "conv-1", "OUTBOUND", "Dạ còn ạ",
                         now - timedelta(hours=2), "out-1")

        rule = {"id": "r1", "delay_minutes": 60, "message": "Anh/chị còn cần shop hỗ trợ không ạ?",
                "enabled": True}
        await self.scheduler._apply_rule(rule)

        rows = self._tasks()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["conversation_id"], "conv-1")
        self.assertEqual(payload["response"], rule["message"])
        self.assertEqual(payload["origin"], "agent")
        self.assertEqual(payload["inbound_watermark"], "in-1")
        self.assertTrue(payload["confirm_send"])

    async def test_a_conversation_still_within_the_delay_is_not_queued(self):
        _register_conversation(self.database, "conv-2")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-2", "INBOUND", "Hi",
                         now - timedelta(minutes=30), "in-2")
        _insert_message(self.database, "conv-2", "OUTBOUND", "Dạ chào bạn",
                         now - timedelta(minutes=5), "out-2")

        await self.scheduler._apply_rule(
            {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}
        )
        self.assertEqual(self._tasks(), [])

    async def test_a_conversation_the_customer_already_answered_is_not_queued(self):
        _register_conversation(self.database, "conv-3")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-3", "OUTBOUND", "Dạ chào bạn",
                         now - timedelta(hours=5), "out-3a")
        _insert_message(self.database, "conv-3", "INBOUND", "Dạ em cảm ơn",
                         now - timedelta(hours=1), "in-3")

        await self.scheduler._apply_rule(
            {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}
        )
        self.assertEqual(self._tasks(), [])

    async def test_a_conversation_with_no_customer_message_ever_is_never_queued(self):
        # Cold outreach guard: only re-engage someone already talking to the
        # shop, never message a stranger who has not written in.
        _register_conversation(self.database, "conv-4")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-4", "OUTBOUND", "Chào bạn",
                         now - timedelta(hours=5), "out-4")

        await self.scheduler._apply_rule(
            {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}
        )
        self.assertEqual(self._tasks(), [])

    async def test_the_same_silence_is_never_queued_twice(self):
        _register_conversation(self.database, "conv-5")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-5", "INBOUND", "Hi",
                         now - timedelta(hours=3), "in-5")
        _insert_message(self.database, "conv-5", "OUTBOUND", "Dạ chào bạn",
                         now - timedelta(hours=2), "out-5")

        rule = {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}
        await self.scheduler._apply_rule(rule)
        await self.scheduler._apply_rule(rule)
        self.assertEqual(len(self._tasks()), 1)

    async def test_a_disabled_rule_never_fires(self):
        _register_conversation(self.database, "conv-6")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-6", "INBOUND", "Hi",
                         now - timedelta(hours=3), "in-6")
        _insert_message(self.database, "conv-6", "OUTBOUND", "Dạ chào bạn",
                         now - timedelta(hours=2), "out-6")

        self.scheduler.policy.set([
            {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": False},
        ])
        await self.scheduler._scan_once()
        self.assertEqual(self._tasks(), [])

    async def test_recipient_falls_back_to_conversation_id_with_no_profile(self):
        _register_conversation(self.database, "conv-7")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-7", "INBOUND", "Hi",
                         now - timedelta(hours=3), "in-7")
        _insert_message(self.database, "conv-7", "OUTBOUND", "Dạ chào bạn",
                         now - timedelta(hours=2), "out-7")

        await self.scheduler._apply_rule(
            {"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}
        )
        payload = json.loads(self._tasks()[0]["payload"])
        self.assertEqual(payload["recipient"], "conv-7")


    async def test_a_rule_not_in_the_shop_language_is_never_sent(self):
        """A Myanmar shop with a Vietnamese rule left from before the switch:
        the rule is skipped rather than sent in a language the shop no longer
        speaks."""
        _register_conversation(self.database, "conv-8")
        now = datetime.now(timezone.utc)
        _insert_message(self.database, "conv-8", "INBOUND", "ဈေးဘယ်လောက်လဲ",
                         now - timedelta(hours=3), "in-8")
        _insert_message(self.database, "conv-8", "OUTBOUND", "ရှိပါတယ်ရှင်",
                         now - timedelta(hours=2), "out-8")
        self.scheduler.reply_language = lambda: "my"

        await self.scheduler._apply_rule({
            "id": "r-vi", "delay_minutes": 60,
            "message": "Anh/chị còn cần shop hỗ trợ không ạ?", "enabled": True,
        })
        self.assertEqual(self._tasks(), [])

        await self.scheduler._apply_rule({
            "id": "r-my", "delay_minutes": 60,
            "message": "ထပ်မံကူညီပေးရန် လိုအပ်ပါသလားရှင်", "enabled": True,
        })
        self.assertEqual(len(self._tasks()), 1)


if __name__ == "__main__":
    unittest.main()
