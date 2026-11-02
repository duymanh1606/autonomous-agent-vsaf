"""One reply with a picture is one bubble, and it keeps the local file.

A reply that carries an attachment is written down twice: once by
`ReplyService.record_sent` the moment it is verified as delivered, and again
when a later scan re-reads the thread. The first version holds the local
catalogue `path` the dashboard can actually serve; the second holds TikTok's
own `src`. Because the content hash includes the DOM position and only the
second version has one, the two used to land as two rows - the operator saw
the same reply twice, once with a picture and once with a broken one.
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.conversation_context import ConversationContextRepository
from tests.support import make_database


CONVERSATION = "0:1:6981093337449300994:7673698693509317649"
REPLY = "Dạ em gửi ảnh sản phẩm ạ"


def message(direction, text, *, order=None, media=()):
    return SimpleNamespace(
        direction=direction, text=text, order=order, media=list(media),
    )


class ReplyRowReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self, account_id="acc_test")
        self.repository = ConversationContextRepository(self.database)

    def _rows(self, direction=None):
        sql = (
            "SELECT direction, text, media_json FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s"
        )
        params = ["acc_test", CONVERSATION]
        if direction:
            sql += " AND direction = %s"
            params.append(direction)
        return self.database.conn.execute(sql + " ORDER BY id", params).fetchall()

    async def _record_sent(self, media):
        """What `ReplyService.record_sent` stores: no DOM position yet."""
        await self.repository.store_visible_messages(
            CONVERSATION, [message("OUTBOUND", REPLY, media=media)], source="dom",
        )

    async def _scan_read(self, media, order=4):
        """What a later scan stores: the same bubble, now with a position."""
        await self.repository.store_visible_messages(
            CONVERSATION,
            [message("OUTBOUND", REPLY, order=order, media=media)],
            source="dom",
        )

    async def test_a_sent_reply_re_read_from_the_dom_stays_one_row(self):
        await self._record_sent([{"kind": "image", "path": "/srv/products/sp001/images/a.png"}])
        await self._scan_read([{"kind": "image", "src": "blob:https://www.tiktok.com/abc"}])

        rows = self._rows("OUTBOUND")
        self.assertEqual(len(rows), 1)
        # The local file wins: it is the only one the dashboard can serve.
        self.assertEqual(
            json.loads(rows[0]["media_json"])[0]["path"],
            "/srv/products/sp001/images/a.png",
        )

    async def test_a_picture_only_the_dom_saw_is_added_to_the_sent_row(self):
        """The reverse case: a reply recorded with no media at all, whose
        image only the re-read thread reveals."""
        await self._record_sent([])
        await self._scan_read([{"kind": "image", "src": "https://cdn.example.com/a.webp"}])

        rows = self._rows("OUTBOUND")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["media_json"])[0]["src"],
            "https://cdn.example.com/a.webp",
        )

    async def test_the_same_reply_sent_twice_is_still_two_bubbles(self):
        await self._record_sent([])
        await self._scan_read([], order=4)
        await self._scan_read([], order=7)

        self.assertEqual(len(self._rows("OUTBOUND")), 2)

    async def test_a_customer_repeating_one_word_keeps_every_occurrence(self):
        """The positional hash exists to stop these being swallowed; the
        reconciliation above must stay out of the INBOUND direction."""
        await self.repository.store_visible_messages(
            CONVERSATION,
            [
                message("INBOUND", "dạ", order=0),
                message("INBOUND", "dạ", order=1),
                message("INBOUND", "dạ", order=2),
            ],
            source="dom",
        )

        self.assertEqual(len(self._rows("INBOUND")), 3)

    async def test_a_dom_read_with_no_sent_row_behind_it_is_stored_as_before(self):
        """The ordinary case - a thread read by the backfill, where nothing
        was ever recorded at send time."""
        await self._scan_read([{"kind": "image", "src": "https://cdn.example.com/a.webp"}])

        rows = self._rows("OUTBOUND")
        self.assertEqual(len(rows), 1)
        self.assertIn("cdn.example.com", rows[0]["media_json"])


    async def test_re_reading_the_same_bubble_again_adds_nothing(self):
        await self._record_sent([{"kind": "image", "path": "/srv/a.png"}])
        await self._scan_read([{"kind": "image", "src": "blob:x"}], order=4)
        await self._scan_read([{"kind": "image", "src": "blob:x"}], order=4)

        self.assertEqual(len(self._rows("OUTBOUND")), 1)

    async def test_a_position_the_backfill_already_stored_keeps_one_row(self):
        """The backfill reads history before anything is sent, so the
        positional row can exist first. The send's own row then has nowhere
        to move to and must be folded into it, not left beside it."""
        await self._scan_read([], order=4)
        await self._record_sent([{"kind": "image", "path": "/srv/a.png"}])
        await self._scan_read([], order=4)

        rows = self._rows("OUTBOUND")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["media_json"])[0]["path"], "/srv/a.png",
        )


if __name__ == "__main__":
    unittest.main()
