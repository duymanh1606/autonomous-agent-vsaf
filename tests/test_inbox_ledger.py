"""The inbox ledger has to survive a restart.

Opening a thread clears its unread marker, so a conversation the agent read but
could not answer never appears in the platform's unread list again. While the
ledger lived in a dictionary, restarting the agent lost those customers for
good - and lost the record of which messages had already been answered, which
is what stops a customer being answered twice.

Runs against a real PostgreSQL schema, created per test; without a reachable
server the tests skip.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.inbox_ledger import InboxLedgerRepository
from tests.support import make_database


class LedgerSurvivesRestart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.ledger = InboxLedgerRepository(self.database)

    async def test_an_unanswered_conversation_comes_back(self):
        await self.ledger.mark_unanswered(
            "thread-1", name="helloworld!!", status="no_inbound_message", attempts=2
        )

        settled, unanswered = await InboxLedgerRepository(self.database).load()

        self.assertEqual(settled, {})
        self.assertEqual(
            unanswered, {"thread-1": {"name": "helloworld!!", "attempts": 2}}
        )

    async def test_an_answered_conversation_is_not_answered_again(self):
        await self.ledger.settle(
            "thread-1", name="helloworld!!", status="sent",
            inbound_message_id="dom-aa61b16c", fingerprint="abc123",
        )

        settled, unanswered = await InboxLedgerRepository(self.database).load()

        self.assertEqual(unanswered, {})
        self.assertEqual(settled["thread-1"]["inbound_message_id"], "dom-aa61b16c")
        self.assertEqual(settled["thread-1"]["fingerprint"], "abc123")

    async def test_answering_replaces_the_unanswered_row(self):
        """A conversation is either waiting or done, never both."""
        await self.ledger.mark_unanswered(
            "thread-1", name="helloworld!!", status="waiting_for_customer", attempts=1
        )
        await self.ledger.settle(
            "thread-1", name="helloworld!!", status="sent",
            inbound_message_id="m9", fingerprint="f9",
        )

        settled, unanswered = await self.ledger.load()

        self.assertEqual(unanswered, {})
        self.assertEqual(list(settled), ["thread-1"])

    async def test_giving_up_stops_the_tracking(self):
        await self.ledger.mark_unanswered(
            "thread-1", name="x", status="no_inbound_message", attempts=4
        )
        await self.ledger.forget("thread-1")

        settled, unanswered = await self.ledger.load()

        self.assertEqual((settled, unanswered), ({}, {}))

    async def test_the_ledger_is_bound_to_one_account(self):
        """Two accounts share the database; neither may read the other's rows."""
        await self.ledger.mark_unanswered(
            "thread-1", name="mine", status="no_inbound_message", attempts=1
        )
        self.database.conn.execute(
            "INSERT INTO accounts(id, platform, username, status) "
            "VALUES ('acc_other', 'tiktok_web', 'other', 'active')"
        )
        self.database.conn.commit()

        settled, unanswered = await InboxLedgerRepository(
            self.database, account_id="acc_other"
        ).load()

        self.assertEqual((settled, unanswered), ({}, {}))


if __name__ == "__main__":
    unittest.main()
