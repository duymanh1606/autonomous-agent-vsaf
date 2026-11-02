"""Which end of a long conversation the dashboard shows.

The bug this locks out was silent in every way that usually catches one: the
query succeeded, the API returned 200, every row it sent was real, and the UI
rendered them faithfully. It ordered oldest-first and took the first `limit`,
so the moment a thread grew past that many messages the newest ones fell
outside the window *permanently* - the conversation froze on screen while the
customer kept writing. Found live on a 241-message thread whose last 41
messages had stopped appearing.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.db_reader import AgentDatabaseReader
from tests.support import make_database

CONVERSATION = "conv-long"


class ConversationMessageWindowTests(unittest.TestCase):
    def setUp(self):
        self.database = make_database(self, account_id="acct-1")
        self.database.conn.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acct-1', 'tiktok_web') "
            "ON CONFLICT DO NOTHING"
        )
        self.database.conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id) "
            "VALUES ('acct-1', %s) ON CONFLICT DO NOTHING",
            (CONVERSATION,),
        )
        self.database.conn.commit()
        self.reader = AgentDatabaseReader(self.database.dsn, account_id="acct-1")
        self.addCleanup(self.reader.close)

    def _messages(self, count):
        """`count` messages a minute apart, oldest first."""
        start = datetime.now(timezone.utc) - timedelta(minutes=count + 1)
        for index in range(count):
            self.database.conn.execute(
                """INSERT INTO conversation_messages
                     (account_id, conversation_id, customer_id, message_id,
                      direction, text, created_at, source)
                   VALUES ('acct-1', %s, 'cust-1', %s, 'INBOUND', %s, %s, 'dom')""",
                (CONVERSATION, f"m{index:04d}", f"tin {index}",
                 start + timedelta(minutes=index)),
            )
        self.database.conn.commit()

    def test_a_thread_longer_than_the_limit_shows_its_newest_messages(self):
        self._messages(120)

        messages = self.reader.conversation(CONVERSATION, limit=100)["messages"]

        self.assertEqual(len(messages), 100)
        # The last message written must be the last one shown - this is the
        # whole bug: it used to be message 99, and 100..119 never appeared.
        self.assertEqual(messages[-1]["text"], "tin 119")
        self.assertEqual(messages[0]["text"], "tin 20")

    def test_messages_stay_in_reading_order(self):
        """Taking the newest N means querying descending; the UI still reads
        top to bottom, so they have to come back ascending."""
        self._messages(120)

        messages = self.reader.conversation(CONVERSATION, limit=100)["messages"]

        stamps = [row["created_at"] for row in messages]
        self.assertEqual(stamps, sorted(stamps))

    def test_a_thread_shorter_than_the_limit_is_returned_whole(self):
        self._messages(5)

        messages = self.reader.conversation(CONVERSATION, limit=100)["messages"]

        self.assertEqual([row["text"] for row in messages],
                         [f"tin {i}" for i in range(5)])

    def test_an_empty_thread_is_not_an_error(self):
        self.assertEqual(self.reader.conversation(CONVERSATION, limit=100)["messages"], [])

    def test_the_internal_ordering_column_is_not_leaked_to_the_api(self):
        """The window is built with the table's `id`; it is a paging detail,
        not part of the message the dashboard renders."""
        self._messages(3)

        messages = self.reader.conversation(CONVERSATION, limit=100)["messages"]

        self.assertTrue(messages)
        self.assertNotIn("id", messages[0])

    def test_one_account_never_sees_another_account_s_thread(self):
        self.database.conn.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acct-2', 'tiktok_web')"
        )
        self.database.conn.commit()
        self._messages(3)

        other = AgentDatabaseReader(self.database.dsn, account_id="acct-2")
        self.addCleanup(other.close)

        self.assertEqual(other.conversation(CONVERSATION, limit=100)["messages"], [])


if __name__ == "__main__":
    unittest.main()
