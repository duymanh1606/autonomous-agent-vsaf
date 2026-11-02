"""Credential-free tests for the one-time `messaging.backfill_inbox` flow.

Reuses `StubContext`/`observation_for` from `test_inbox_scan_flow.py` - same
harness shape as `test_inbox_scan_human_takeover.py`.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.result import ActionResult
from models.tasks import Task
from platforms.tiktok.inbox_backfill import (
    BACKFILL_MARKER_KEY, IMPORTED_KEY_PREFIX, InboxBackfillSkill,
)
from platforms.tiktok.screens import TikTokScreen
from tests.test_inbox_scan_flow import StubContext, observation_for


class FakeLedger:
    """In-memory stand-in for `InboxLedgerRepository`, same call shape."""

    def __init__(self):
        self.settled = {}

    async def load(self):
        return dict(self.settled), {}

    async def settle(self, key, *, name, status, inbound_message_id, fingerprint):
        self.settled[key] = {
            "inbound_message_id": inbound_message_id,
            "fingerprint": fingerprint,
            "status": status,
        }


class FakeReplyProvider:
    def __init__(self):
        self.persisted = []

    async def persist_thread(self, context):
        self.persisted.append(context)


class FakeRepository:
    def __init__(self, active_capability=None):
        self.submitted = []
        self._active = active_capability

    async def has_active_capability(self, capability):
        return capability == self._active

    async def submit(self, task):
        self.submitted.append(task)


def backfill_task():
    return Task(
        account_id="acc_test", id="inbox-backfill-1",
        capability="messaging.backfill_inbox", platform="tiktok_web",
    )


def context_with_conversations(names, *, open_results=None):
    """A Messages-panel context whose `list_all_conversations` returns `names`.

    `open_results`: name -> ActionResult override for
    `platform.open_conversation`; defaults to a fresh one-message thread.
    """
    open_results = open_results or {}

    def open_conversation(action):
        target = action.args["target"]
        if target in open_results:
            return open_results[target]
        return (
            ActionResult(success=True, data={}),
            observation_for(
                TikTokScreen.CONVERSATION,
                messages=[{"id": f"m-{target}", "text": f"hi from {target}", "direction": "INBOUND"}],
                active=target,
            ),
        )

    return StubContext(
        observation_for(TikTokScreen.MESSAGES),
        {
            "platform.list_all_conversations": ActionResult(
                success=True, data={"tool": {"conversations": list(names)}}
            ),
            "platform.open_conversation": open_conversation,
        },
    )


class InboxBackfillSkillTests(unittest.IsolatedAsyncioTestCase):
    def _skill(self, ledger=None, reply_provider=None, **kwargs):
        return InboxBackfillSkill(
            reply_provider=reply_provider or FakeReplyProvider(),
            ledger=ledger if ledger is not None else FakeLedger(),
            **kwargs,
        )

    async def test_already_done_short_circuits_without_any_action(self):
        ledger = FakeLedger()
        ledger.settled[BACKFILL_MARKER_KEY] = {
            "status": "backfill_completed", "inbound_message_id": "backfill", "fingerprint": "",
        }
        skill = self._skill(ledger=ledger)
        context = context_with_conversations(["alice"])

        result = await skill.execute(backfill_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "already_done")
        self.assertEqual(context.actions, [])

    async def test_imports_every_conversation_and_marks_the_account_done(self):
        reply_provider = FakeReplyProvider()
        ledger = FakeLedger()
        skill = self._skill(ledger=ledger, reply_provider=reply_provider, batch_size=20)
        context = context_with_conversations(["alice", "bob"])

        result = await skill.execute(backfill_task(), context)

        self.assertTrue(result.success)
        self.assertEqual(result.data["conversations_found"], 2)
        self.assertEqual(result.data["conversations_imported"], 2)
        self.assertEqual(result.data["conversations_remaining"], 0)
        self.assertEqual(
            sorted(c.customer_name for c in reply_provider.persisted), ["alice", "bob"],
        )
        # Real history reached persist_thread, not an empty/placeholder one.
        self.assertTrue(all(c.history for c in reply_provider.persisted))
        self.assertTrue(await skill.already_done())
        self.assertIn(IMPORTED_KEY_PREFIX + "alice", ledger.settled)
        self.assertIn(IMPORTED_KEY_PREFIX + "bob", ledger.settled)

    async def test_never_calls_reply_or_send_actions(self):
        """The whole point: history is stored, nothing is ever sent."""
        reply_provider = FakeReplyProvider()
        skill = self._skill(reply_provider=reply_provider)
        context = context_with_conversations(["alice"])

        await skill.execute(backfill_task(), context)

        self.assertNotIn("platform.send_message", context.actions)
        self.assertNotIn("platform.focus_message_input", context.actions)

    async def test_batch_smaller_than_pending_queues_a_follow_up_task_without_marking_done(self):
        skill = self._skill(batch_size=1)
        context = context_with_conversations(["alice", "bob"])
        context.memory = SimpleNamespace(repository=FakeRepository())

        result = await skill.execute(backfill_task(), context)

        self.assertEqual(result.data["conversations_imported"], 1)
        self.assertEqual(result.data["conversations_remaining"], 1)
        self.assertFalse(await skill.already_done())
        self.assertEqual(len(context.memory.repository.submitted), 1)
        follow_up = context.memory.repository.submitted[0]
        self.assertEqual(follow_up.capability, "messaging.backfill_inbox")
        self.assertEqual(follow_up.account_id, "acc_test")

    async def test_a_follow_up_is_not_queued_while_one_is_already_active(self):
        skill = self._skill(batch_size=1)
        context = context_with_conversations(["alice", "bob"])
        context.memory = SimpleNamespace(
            repository=FakeRepository(active_capability="messaging.backfill_inbox")
        )

        await skill.execute(backfill_task(), context)

        self.assertEqual(context.memory.repository.submitted, [])

    async def test_a_conversation_that_fails_to_open_is_retried_not_marked_imported(self):
        ledger = FakeLedger()
        skill = self._skill(ledger=ledger, batch_size=20)
        context = context_with_conversations(
            ["alice", "broken"],
            open_results={"broken": ActionResult(success=False, error="not found")},
        )

        result = await skill.execute(backfill_task(), context)

        self.assertEqual(result.data["conversations_imported"], 1)
        self.assertEqual(result.data["conversations_failed"], 1)
        self.assertEqual(result.data["conversations_remaining"], 1)
        self.assertFalse(await skill.already_done())
        self.assertNotIn(IMPORTED_KEY_PREFIX + "broken", ledger.settled)

    async def test_second_batch_skips_conversations_already_imported(self):
        ledger = FakeLedger()
        reply_provider = FakeReplyProvider()
        # batch_size=1 leaves "bob" pending (not done) after the first run,
        # matching a real multi-batch continuation instead of a completed one.
        skill = self._skill(ledger=ledger, reply_provider=reply_provider, batch_size=1)
        await skill.execute(backfill_task(), context_with_conversations(["alice", "bob"]))
        self.assertEqual([c.customer_name for c in reply_provider.persisted], ["alice"])
        self.assertFalse(await skill.already_done())

        # A fresh skill instance (e.g. after a restart) restores the same
        # ledger and must not re-import what is already there.
        second = InboxBackfillSkill(reply_provider=reply_provider, ledger=ledger, batch_size=20)
        result = await second.execute(
            backfill_task(), context_with_conversations(["alice", "bob"])
        )

        self.assertEqual(result.data["conversations_imported"], 1)
        self.assertEqual(
            sorted(c.customer_name for c in reply_provider.persisted), ["alice", "bob"],
        )
        self.assertTrue(await second.already_done())

    async def test_platform_other_than_tiktok_web_is_rejected(self):
        skill = self._skill()
        task = Task(
            account_id="acc_test", id="t1",
            capability="messaging.backfill_inbox", platform="other",
        )
        result = await skill.execute(task, context_with_conversations(["alice"]))
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
