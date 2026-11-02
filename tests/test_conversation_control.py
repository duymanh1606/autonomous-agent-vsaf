"""`ConversationControlRepository` behavior and account isolation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.conversation_control import ConversationControlRepository
from tests.support import make_database


def register_conversation(database, conversation_id):
    database.conn.execute(
        "INSERT INTO conversations(account_id, platform_conversation_id) "
        "VALUES (%s, %s)",
        (database.account_id, conversation_id),
    )
    database.conn.commit()


class ConversationControlRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.repo = ConversationControlRepository(self.database)
        register_conversation(self.database, "conv-1")

    async def test_no_row_defaults_to_auto(self):
        self.assertEqual(await self.repo.mode("conv-1"), "AUTO")
        self.assertTrue(await self.repo.agent_may_reply("conv-1"))
        self.assertFalse(await self.repo.human_may_send("conv-1"))

    async def test_manual_takeover_from_auto_needs_no_escalation(self):
        result = await self.repo.takeover("conv-1", "operator-1")
        self.assertEqual(result["mode"], "HUMAN")
        self.assertEqual(result["operator_id"], "operator-1")
        self.assertEqual(result["takeover_source"], "MANUAL")
        self.assertFalse(await self.repo.agent_may_reply("conv-1"))
        self.assertTrue(await self.repo.human_may_send("conv-1"))

    async def test_manual_takeover_from_waiting_human(self):
        await self.repo.wait_for_human("conv-1", "requires_human")
        self.assertEqual(await self.repo.mode("conv-1"), "WAITING_HUMAN")
        result = await self.repo.takeover("conv-1", "operator-1")
        self.assertEqual(result["mode"], "HUMAN")

    async def test_escalation_moves_auto_to_waiting_human(self):
        result = await self.repo.wait_for_human("conv-1", "requires_human")
        self.assertEqual(result["mode"], "WAITING_HUMAN")
        self.assertEqual(result["takeover_source"], "ESCALATION")

    async def test_escalation_never_downgrades_human(self):
        await self.repo.takeover("conv-1", "operator-1")
        result = await self.repo.wait_for_human("conv-1", "requires_human")
        self.assertEqual(result["mode"], "HUMAN")
        self.assertEqual(result["takeover_source"], "MANUAL")

    async def test_release_from_human_returns_to_auto(self):
        await self.repo.takeover("conv-1", "operator-1")
        result = await self.repo.release("conv-1")
        self.assertEqual(result["mode"], "AUTO")
        self.assertIsNone(result["operator_id"])
        self.assertIsNone(result["takeover_source"])
        self.assertIsNone(result["reason"])
        self.assertTrue(await self.repo.agent_may_reply("conv-1"))

    async def test_release_from_waiting_human_returns_to_auto(self):
        await self.repo.wait_for_human("conv-1", "requires_human")
        result = await self.repo.release("conv-1")
        self.assertEqual(result["mode"], "AUTO")


def _backdate(database, conversation_id, minutes):
    database.conn.execute(
        "UPDATE conversation_control SET updated_at = "
        "CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute') "
        "WHERE account_id = %s AND conversation_id = %s",
        (minutes, database.account_id, conversation_id),
    )
    database.conn.commit()


class ConversationControlTtlExpiryTests(unittest.IsolatedAsyncioTestCase):
    """A forgotten Release or an unresolved escalation must not lock a
    customer out of the agent forever - see `_expired` in
    storage/conversation_control.py."""

    def setUp(self):
        self.database = make_database(self)
        register_conversation(self.database, "conv-1")

    async def test_human_takeover_survives_within_its_ttl(self):
        repo = ConversationControlRepository(
            self.database, human_takeover_ttl_minutes=60,
        )
        await repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 59)
        self.assertEqual(await repo.mode("conv-1"), "HUMAN")
        self.assertFalse(await repo.agent_may_reply("conv-1"))

    async def test_human_takeover_auto_releases_past_its_ttl(self):
        repo = ConversationControlRepository(
            self.database, human_takeover_ttl_minutes=60,
        )
        await repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 61)
        self.assertEqual(await repo.mode("conv-1"), "AUTO")
        self.assertTrue(await repo.agent_may_reply("conv-1"))
        # The release is durable, not just an in-memory read.
        fresh = ConversationControlRepository(
            self.database, human_takeover_ttl_minutes=60,
        )
        self.assertEqual(await fresh.mode("conv-1"), "AUTO")

    async def test_sending_renews_a_takeover_the_operator_is_using(self):
        """Reported: an hour after "Tiếp quản" every manual message failed -
        the TTL ran from the takeover and nothing the operator did moved it."""
        repo = ConversationControlRepository(self.database, human_takeover_ttl_minutes=60)
        await repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 59)
        self.assertEqual((await repo.renew_human("conv-1"))["mode"], "HUMAN")
        _backdate(self.database, "conv-1", 30)  # 30 minutes after the renewal
        self.assertEqual(await repo.mode("conv-1"), "HUMAN")

    async def test_renewing_does_not_revive_a_lapsed_takeover(self):
        repo = ConversationControlRepository(self.database, human_takeover_ttl_minutes=60)
        await repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 61)
        self.assertEqual((await repo.renew_human("conv-1"))["mode"], "AUTO")
        self.assertEqual(await repo.mode("conv-1"), "AUTO")

    async def test_renewing_never_turns_the_agent_off(self):
        repo = ConversationControlRepository(self.database)
        self.assertEqual((await repo.renew_human("conv-1"))["mode"], "AUTO")
        await repo.wait_for_human("conv-1", "escalated")
        self.assertEqual((await repo.renew_human("conv-1"))["mode"], "WAITING_HUMAN")

    async def test_waiting_human_auto_releases_past_its_shorter_ttl(self):
        repo = ConversationControlRepository(
            self.database, waiting_human_ttl_minutes=20,
        )
        await repo.wait_for_human("conv-1", "requires_human")
        _backdate(self.database, "conv-1", 21)
        self.assertEqual(await repo.mode("conv-1"), "AUTO")
        self.assertTrue(await repo.agent_may_reply("conv-1"))

    async def test_zero_ttl_disables_auto_release(self):
        repo = ConversationControlRepository(
            self.database, human_takeover_ttl_minutes=0,
        )
        await repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 10_000)
        self.assertEqual(await repo.mode("conv-1"), "HUMAN")


class WritesAgainstALapsedClaimTests(unittest.IsolatedAsyncioTestCase):
    """Every write guards on the mode stored in SQL, while expiry is decided
    in Python - so a claim this repository already considers gone still reads
    as 'HUMAN' to those guards until something calls `get`.

    These go through the writes with nothing having read the row since it
    lapsed, which is exactly how the dashboard reaches them: the operator's
    click is the next thing to touch the row. Reading it first (as the other
    TTL tests do) releases it and hides the whole class of bug.
    """

    def setUp(self):
        self.database = make_database(self)
        register_conversation(self.database, "conv-1")
        self.repo = ConversationControlRepository(
            self.database, human_takeover_ttl_minutes=60,
            waiting_human_ttl_minutes=20,
        )

    def _stored_mode(self):
        row = self.database.conn.execute(
            "SELECT mode FROM conversation_control "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.database.account_id, "conv-1"),
        ).fetchone()
        return dict(row)["mode"]

    async def test_taking_over_a_lapsed_claim_gives_the_operator_the_conversation(self):
        """The operator clicking Take over on a conversation whose previous
        claim quietly aged out must end up owning it. This used to raise
        TypeError *and* leave the conversation in AUTO - so the click both
        failed and handed the customer back to the agent, which is the one
        outcome a takeover must never produce."""
        await self.repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 61)
        self.assertEqual(self._stored_mode(), "HUMAN")  # nothing has read it yet

        result = await self.repo.takeover("conv-1", "operator-2")

        self.assertEqual(result["mode"], "HUMAN")
        self.assertEqual(result["operator_id"], "operator-2")
        self.assertFalse(await self.repo.agent_may_reply("conv-1"))

    async def test_a_live_claim_still_belongs_to_the_first_operator(self):
        """The lapsed-claim fix must not turn takeover into a way to steal an
        active one: within its TTL the first operator keeps the conversation."""
        await self.repo.takeover("conv-1", "operator-1")

        result = await self.repo.takeover("conv-1", "operator-2")

        self.assertEqual(result["mode"], "HUMAN")
        self.assertEqual(result["operator_id"], "operator-1")

    async def test_escalating_past_a_lapsed_claim_records_waiting_human(self):
        """An escalation arriving after the claim aged out is a conversation
        nobody is holding any more. Left as 'HUMAN' it would silently restart
        the very TTL that was about to free the customer, and no
        WAITING_HUMAN would be recorded for an escalation that really did
        happen."""
        await self.repo.takeover("conv-1", "operator-1")
        _backdate(self.database, "conv-1", 61)
        self.assertEqual(self._stored_mode(), "HUMAN")

        result = await self.repo.wait_for_human("conv-1", "requires_human")

        self.assertEqual(result["mode"], "WAITING_HUMAN")
        self.assertEqual(result["takeover_source"], "ESCALATION")

    async def test_escalation_still_never_downgrades_a_live_claim(self):
        await self.repo.takeover("conv-1", "operator-1")

        result = await self.repo.wait_for_human("conv-1", "requires_human")

        self.assertEqual(result["mode"], "HUMAN")
        self.assertEqual(result["takeover_source"], "MANUAL")


class ConversationControlAccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_accounts_with_the_same_conversation_id_are_independent(self):
        database_a = make_database(self, account_id="acc_a")
        database_b = make_database(self, account_id="acc_b")
        register_conversation(database_a, "shared-conv")
        register_conversation(database_b, "shared-conv")
        repo_a = ConversationControlRepository(database_a)
        repo_b = ConversationControlRepository(database_b)

        await repo_a.takeover("shared-conv", "operator-a")

        self.assertEqual(await repo_a.mode("shared-conv"), "HUMAN")
        self.assertEqual(await repo_b.mode("shared-conv"), "AUTO")


if __name__ == "__main__":
    unittest.main()
