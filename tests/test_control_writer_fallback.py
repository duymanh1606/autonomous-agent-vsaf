"""A stopped workspace must not make a conversation untakeable.

`conversation_control` used to be writable only through the control server,
which lives inside the agent process. A workspace that was stopped - crashed,
or waiting for a restart after a deploy - therefore answered 503 to every
takeover, and a conversation sitting in `WAITING_HUMAN` could not be handed to
the person it was escalated to. PostgreSQL was up the whole time and the
supervisor already held its DSN.

The supervisor now writes the row itself when, and only when, the agent cannot
be asked. These tests pin both halves: the fallback works, and it never
displaces the agent on the normal path.
"""

import sys
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import main
from dashboard_api.control_writer import ConversationControlWriter
from storage.conversation_control import ConversationControlRepository
from tests.support import make_database


CONV = "0:1:6981093337449300994:7673698693509317649"


class SupervisorWritesControlRowsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self, account_id="acc_test")
        self.database.conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id, platform, "
            "customer_id) VALUES (%s, %s, 'tiktok_web', 'cust-1')",
            (self.database.account_id, CONV),
        )
        self.database.conn.commit()
        self.agent_view = ConversationControlRepository(self.database)
        self.writer = ConversationControlWriter(
            self.database.dsn, account_id="acc_test"
        )
        self.addCleanup(self.writer.close)

    async def test_an_escalated_conversation_can_be_taken_over_with_no_agent(self):
        await self.agent_view.wait_for_human(CONV, "reply_withheld:routed_to_human")
        self.database.conn.commit()

        row = await self.writer.takeover(CONV, "operator-1")

        self.assertEqual(row["mode"], "HUMAN")
        self.assertEqual(row["operator_id"], "operator-1")
        # And the agent sees it, because every mode check reads the row fresh.
        self.assertEqual(await self.agent_view.mode(CONV), "HUMAN")

    async def test_the_write_is_durable_and_not_left_in_a_transaction(self):
        """A row the supervisor wrote but never committed would be invisible to
        the agent - the very process that has to respect it."""
        await self.writer.takeover(CONV, "operator-1")

        fresh = ConversationControlRepository(self.database)
        self.assertEqual(await fresh.mode(CONV), "HUMAN")

    async def test_an_agent_side_takeover_is_committed_for_the_supervisor(self):
        """The agent's connection is not autocommit and a prior SELECT has
        already opened a transaction, so `conn.transaction()` alone was a
        savepoint: the supervisor never saw the takeover, and an unrelated
        rollback on the agent's connection erased it."""
        await self.agent_view.mode(CONV)
        await self.agent_view.takeover(CONV, "operator-1")
        self.database.conn.rollback()

        self.assertEqual((await self.writer.renew_human(CONV))["mode"], "HUMAN")
        self.assertEqual(await self.agent_view.mode(CONV), "HUMAN")

    async def test_release_hands_the_conversation_back(self):
        await self.writer.takeover(CONV, "operator-1")

        row = await self.writer.release(CONV)

        self.assertEqual(row["mode"], "AUTO")
        self.assertEqual(await self.agent_view.mode(CONV), "AUTO")

    async def test_a_live_claim_by_somebody_else_still_wins(self):
        """The fallback reuses the repository, so takeover stays idempotent
        rather than reassigning a conversation somebody already holds."""
        await self.agent_view.takeover(CONV, "operator-first")
        self.database.conn.commit()

        row = await self.writer.takeover(CONV, "operator-second")

        self.assertEqual(row["operator_id"], "operator-first")


class OnlyWhenTheAgentCannotAnswerTests(unittest.IsolatedAsyncioTestCase):
    """The agent stays the primary path: it owns the events and its own view."""

    ACCOUNT = SimpleNamespace(id="acc_test", control_port=28999)

    async def test_the_agent_answer_is_used_and_nothing_is_written_directly(self):
        writer = mock.Mock()
        with mock.patch.object(
            main, "_control", new=mock.AsyncMock(return_value={"mode": "HUMAN"})
        ), mock.patch.object(main, "_control_writer", return_value=writer):
            row, wrote_directly = await main._control_or_write(
                self.ACCOUNT, "/control/x", {}, lambda w: w.takeover("c", "op"),
            )

        self.assertEqual(row, {"mode": "HUMAN"})
        self.assertFalse(wrote_directly)
        self.assertEqual(writer.method_calls, [])

    async def test_a_503_falls_back_to_the_direct_write(self):
        writer = SimpleNamespace(
            takeover=mock.AsyncMock(return_value={"mode": "HUMAN"})
        )
        failure = main.HTTPException(status_code=503, detail="agent not reachable")
        with mock.patch.object(
            main, "_control", new=mock.AsyncMock(side_effect=failure)
        ), mock.patch.object(main, "_control_writer", return_value=writer):
            row, wrote_directly = await main._control_or_write(
                self.ACCOUNT, "/control/x", {}, lambda w: w.takeover("c", "op"),
            )

        self.assertEqual(row, {"mode": "HUMAN"})
        self.assertTrue(wrote_directly)
        writer.takeover.assert_awaited_once_with("c", "op")

    async def test_any_other_failure_is_reported_instead_of_worked_around(self):
        """A runtime with no conversation control answers 501, and a handler
        error is an answer too. Writing the row anyway would paper over a real
        disagreement between the two processes."""
        for status in (501, 409, 500):
            failure = main.HTTPException(status_code=status, detail="nope")
            writer = mock.Mock()
            with mock.patch.object(
                main, "_control", new=mock.AsyncMock(side_effect=failure)
            ), mock.patch.object(main, "_control_writer", return_value=writer):
                with self.assertRaises(main.HTTPException) as raised:
                    await main._control_or_write(
                        self.ACCOUNT, "/control/x", {}, lambda w: w.takeover("c", "op"),
                    )
            self.assertEqual(raised.exception.status_code, status)
            self.assertEqual(writer.method_calls, [])


if __name__ == "__main__":
    unittest.main()
