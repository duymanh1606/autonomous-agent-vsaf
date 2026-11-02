"""What the inbox scan already knows about each conversation, made durable.

The scan keeps two facts per conversation:

* **settled** - which inbound message was already answered, so a thread that
  re-renders (or an unread marker the platform sets again) never gets a second
  answer to the same question;
* **unanswered** - a thread that was opened but not answered, and how many
  times the agent has come back to it. Opening a thread clears its unread
  marker, so the platform's own list will never show that customer again: this
  row is the only way back to them.

Both used to live in dictionaries on the skill, which meant a restart silently
lost every customer the agent had read but not answered, and forgot which
messages it had already replied to. Both facts belong in the database the
project already runs.

Bound to one account like every other repository: `account_id` comes from the
database the store was opened for and is injected into every statement.
"""

import json
import logging

from models.account import require_account_id


logger = logging.getLogger(__name__)

SETTLED = "SETTLED"
UNANSWERED = "UNANSWERED"


class InboxLedgerRepository:
    def __init__(self, database, *, account_id=None):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database

    # ------------------------------------------------------------- reading
    async def load(self) -> tuple[dict, dict]:
        """`(settled, unanswered)` in the shape the skill keeps in memory.

        Returning the skill's own dict shapes keeps the durability concern
        here: the scan logic reads exactly what it read before, and needs no
        knowledge of how the rows are stored.
        """
        rows = self.database.conn.execute(
            "SELECT conversation_key, conversation_name, state, status, attempts, "
            "inbound_message_id, inbound_fingerprint FROM inbox_ledger "
            "WHERE account_id = %s",
            (self.account_id,),
        ).fetchall()
        settled, unanswered = {}, {}
        for row in rows:
            key = row["conversation_key"]
            if row["state"] == SETTLED:
                settled[key] = {
                    "inbound_message_id": row["inbound_message_id"],
                    "fingerprint": row["inbound_fingerprint"],
                    "status": row["status"],
                }
            else:
                unanswered[key] = {
                    "name": row["conversation_name"] or key,
                    "attempts": int(row["attempts"] or 0),
                }
        logger.info(
            "[LEDGER] account=%s restored settled=%s unanswered=%s",
            self.account_id, len(settled), len(unanswered),
        )
        return settled, unanswered

    # ------------------------------------------------------------- writing
    async def settle(self, key, *, name, status, inbound_message_id, fingerprint):
        """This conversation is answered up to `inbound_message_id`."""
        self._upsert(
            key, name=name, state=SETTLED, status=status, attempts=0,
            inbound_message_id=inbound_message_id, fingerprint=fingerprint,
        )

    async def mark_unanswered(self, key, *, name, status, attempts):
        """Read but not answered; `attempts` is how often we have come back."""
        self._upsert(
            key, name=name, state=UNANSWERED, status=status, attempts=attempts,
            inbound_message_id="", fingerprint="",
        )

    async def forget(self, key):
        """Stop tracking this conversation - the agent gave up on it."""
        key = str(key or "").strip()
        if not key:
            return
        self.database.conn.execute(
            "DELETE FROM inbox_ledger WHERE account_id = %s AND conversation_key = %s",
            (self.account_id, key),
        )
        self.database.conn.commit()

    async def save_pre_accept_snapshot(
        self, key, *, username, conversation_id, messages, classification,
        status="PENDING",
    ):
        """Persist request content before Accept can make it disappear."""
        key = str(key or username or conversation_id or "").strip().casefold().lstrip("@")
        if not key:
            raise ValueError("message request snapshot requires a stable key")
        payload = json.dumps(list(messages or ()), ensure_ascii=False)
        self.database.conn.execute(
            "INSERT INTO message_request_snapshots("
            "account_id, request_key, conversation_id, username, snapshot_json, "
            "classification, status) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(account_id, request_key) DO UPDATE SET "
            "conversation_id = EXCLUDED.conversation_id, username = EXCLUDED.username, "
            "snapshot_json = EXCLUDED.snapshot_json, "
            "classification = EXCLUDED.classification, status = EXCLUDED.status, "
            "updated_at = CURRENT_TIMESTAMP",
            (
                self.account_id, key, str(conversation_id or ""), str(username or ""),
                payload, str(classification or "unknown"), str(status or "PENDING"),
            ),
        )
        self.database.conn.commit()

    async def mark_request_accepted(self, key):
        key = str(key or "").strip().casefold().lstrip("@")
        updated = self.database.conn.execute(
            "UPDATE message_request_snapshots SET status = 'ACCEPTED', "
            "accepted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE account_id = %s AND request_key = %s",
            (self.account_id, key),
        )
        self.database.conn.commit()
        return updated.rowcount == 1

    async def load_pre_accept_snapshot(self, key):
        key = str(key or "").strip().casefold().lstrip("@")
        row = self.database.conn.execute(
            "SELECT * FROM message_request_snapshots "
            "WHERE account_id = %s AND request_key = %s",
            (self.account_id, key),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["messages"] = json.loads(result.pop("snapshot_json") or "[]")
        return result

    def _upsert(self, key, *, name, state, status, attempts, inbound_message_id, fingerprint):
        key = str(key or "").strip()
        if not key:
            return
        # One row per conversation: a conversation is either settled or waiting
        # to be revisited, never both, so the newer fact replaces the older one.
        self.database.conn.execute(
            "INSERT INTO inbox_ledger(account_id, conversation_key, conversation_name, "
            "state, status, attempts, inbound_message_id, inbound_fingerprint) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(account_id, conversation_key) DO UPDATE SET "
            "conversation_name = EXCLUDED.conversation_name, "
            "state = EXCLUDED.state, status = EXCLUDED.status, "
            "attempts = EXCLUDED.attempts, "
            "inbound_message_id = EXCLUDED.inbound_message_id, "
            "inbound_fingerprint = EXCLUDED.inbound_fingerprint, "
            "updated_at = CURRENT_TIMESTAMP",
            (
                self.account_id, key, str(name or ""), state, str(status or ""),
                int(attempts or 0), str(inbound_message_id or ""), str(fingerprint or ""),
            ),
        )
        self.database.conn.commit()
