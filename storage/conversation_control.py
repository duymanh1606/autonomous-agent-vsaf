import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from models.account import require_account_id

logger = logging.getLogger(__name__)


class ConversationControlRepository:
    def __init__(
        self, database, *, account_id=None,
        human_takeover_ttl_minutes=60,
        waiting_human_ttl_minutes=20,
    ):
        resolved = (
            account_id
            if account_id is not None
            else getattr(database, "account_id", None)
        )

        self.account_id = require_account_id(resolved)
        self.database = database
        # A takeover/escalation has no heartbeat: nothing else ever touches
        # `updated_at` while a human is quietly working a conversation
        # through TikTok directly. These TTLs are therefore a last-resort
        # safety net, not an activity timeout - they exist only so a forgotten
        # "Release" click or an unresolved escalation cannot leave a customer
        # abandoned forever. HUMAN gets the longer window because someone
        # explicitly claimed the conversation and may still be mid-reply;
        # WAITING_HUMAN is shorter because nobody has necessarily even seen
        # it yet, and reverting it to AUTO does not force a reply - the next
        # scan re-runs the same safety analysis and re-escalates on its own
        # if the content is still genuinely unsafe.
        self.human_takeover_ttl = timedelta(
            minutes=max(0.0, float(human_takeover_ttl_minutes))
        )
        self.waiting_human_ttl = timedelta(
            minutes=max(0.0, float(waiting_human_ttl_minutes))
        )

    @contextmanager
    def _write(self):
        """One durable write, whatever connection this repository was given.

        The agent's own connection is not autocommit, and any earlier SELECT
        on it has already opened a transaction - there `conn.transaction()` is
        only a savepoint and commits nothing. A takeover written that way was
        visible to the agent alone, held its row lock until some unrelated
        repository happened to commit, and was lost outright when one rolled
        back instead. The explicit commit is what makes the mode real for the
        supervisor and for every later read.
        """
        conn = self.database.conn
        with conn.transaction():
            yield
        if not conn.autocommit:
            conn.commit()

    async def get(self, conversation_id: str) -> dict:
        row = self.database.conn.execute(
            """
            SELECT *
            FROM conversation_control
            WHERE account_id = %s
                AND conversation_id = %s
            """,
            (
                self.account_id,
                conversation_id,
            ),
        ).fetchone()

        if row is None:
            return {
                "account_id": self.account_id,
                "conversation_id": conversation_id,
                "mode": "AUTO",
                "operator_id": None,
                "takeover_source": None,
                "reason": None,
            }

        row = dict(row)
        if self._expired(row):
            logger.warning(
                "[control] takeover expired mode=%s conversation=%s "
                "operator=%s updated_at=%s; auto-released back to AUTO",
                row.get("mode"), conversation_id, row.get("operator_id"),
                row.get("updated_at"),
            )
            return await self.release(conversation_id)
        return row

    def _expired(self, row: dict) -> bool:
        mode = row.get("mode")
        if mode not in ("HUMAN", "WAITING_HUMAN"):
            return False
        ttl = (
            self.human_takeover_ttl if mode == "HUMAN"
            else self.waiting_human_ttl
        )
        if ttl <= timedelta(0):
            return False
        updated_at = row.get("updated_at")
        if updated_at is None:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated_at >= ttl

    async def mode(self, conversation_id: str) -> str:
        return (await self.get(conversation_id))["mode"]

    async def agent_may_reply(self, conversation_id: str) -> bool:
        return await self.mode(conversation_id) == "AUTO"

    async def human_may_send(self, conversation_id: str) -> bool:
        return await self.mode(conversation_id) == "HUMAN"

    async def _settle_expiry(self, conversation_id: str) -> dict:
        """The row as this repository actually judges it, persisted.

        Every write below guards on the *stored* mode, while expiry is decided
        here in Python - so a claim this repository already considers gone
        still reads as 'HUMAN' to SQL until something calls `get`. Reading
        through `get` first collapses that split: it releases a lapsed claim
        durably, so the guards that follow compare against a mode that is
        actually current.
        """
        return await self.get(conversation_id)

    async def takeover(
            self,
            conversation_id: str,
            operator_id: str = "local_operator",
    ):
        # Settle expiry before the guard below reads the stored mode. Skipping
        # this cost an operator the conversation entirely: the guard saw a
        # lapsed 'HUMAN', blocked the write and returned nothing; the fallback
        # read then released that lapsed claim to AUTO; and the takeover both
        # raised TypeError on `dict(None)` and handed the customer back to the
        # agent the operator was trying to take them from.
        current = await self._settle_expiry(conversation_id)
        if current["mode"] == "HUMAN":
            # A live claim stands. Takeover is idempotent by design: the first
            # operator keeps the conversation, so a second click reports that
            # claim rather than reassigning it.
            return current

        with self._write():
            row = self.database.conn.execute(
                """
                INSERT INTO conversation_control(
                    account_id,
                    conversation_id,
                    mode,
                    operator_id,
                    takeover_source,
                    taken_over_at,
                    updated_at
                )
                VALUES (
                    %s, %s, 'HUMAN', %s, 'MANUAL',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                
                ON CONFLICT(account_id, conversation_id)
                DO UPDATE SET
                    mode = 'HUMAN',
                    operator_id = excluded.operator_id,
                    takeover_source = 'MANUAL',
                    taken_over_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                    
                WHERE conversation_control.mode <> 'HUMAN'
                
                RETURNING *
                """,
                (
                    self.account_id,
                    conversation_id,
                    operator_id,
                ),
            ).fetchone()

        if row is None:
            # The guard still blocked the write even though the read above saw
            # no live claim: another operator claimed it in between. Their
            # claim is the truth, and reporting it is the honest answer - this
            # must never fall through to `dict(None)`.
            return await self.get(conversation_id)

        return dict(row)

    async def renew_human(self, conversation_id: str) -> dict:
        """Restart the takeover clock of a conversation a person holds.

        The HUMAN TTL is a safety net for a takeover someone forgot about,
        counted from `updated_at`. Nothing moved that clock while the operator
        was actively chatting, so an hour after "Tiếp quản" every message they
        sent was refused (reported: a row of "Gửi thất bại"). Sending renews
        it. A claim that has already lapsed is settled (released) and returned
        as it now is - the caller tells the operator to take over again rather
        than silently taking it back.
        """
        current = await self._settle_expiry(conversation_id)
        if current["mode"] != "HUMAN":
            return current
        with self._write():
            row = self.database.conn.execute(
                "UPDATE conversation_control SET updated_at = CURRENT_TIMESTAMP "
                "WHERE account_id = %s AND conversation_id = %s AND mode = 'HUMAN' "
                "RETURNING *",
                (self.account_id, conversation_id),
            ).fetchone()
        return dict(row) if row else await self.get(conversation_id)

    async def wait_for_human(
            self,
            conversation_id: str,
            reason: str,
    ):
        # Same reason as `takeover`: the CASE below reads the stored mode, so
        # without settling expiry first a claim that lapsed hours ago still
        # outranks this escalation. The row would stay 'HUMAN' with a freshly
        # bumped `updated_at` - restarting the very TTL that was about to free
        # the customer, and recording no WAITING_HUMAN for the escalation that
        # actually happened.
        await self._settle_expiry(conversation_id)

        with self._write():
            row = self.database.conn.execute(
                """
                INSERT INTO conversation_control(
                    account_id,
                    conversation_id,
                    mode,
                    takeover_source,
                    reason,
                    updated_at
                )
                VALUES (
                    %s, %s,
                    'WAITING_HUMAN',
                    'ESCALATION',
                    %s,
                    CURRENT_TIMESTAMP
                )
                
                ON CONFLICT(account_id, conversation_id)
                DO UPDATE SET
                    mode = CASE
                        WHEN conversation_control.mode = 'HUMAN'
                            THEN 'HUMAN'
                        ELSE 'WAITING_HUMAN'
                    END,
                    takeover_source = CASE
                        WHEN conversation_control.mode = 'HUMAN'
                            THEN conversation_control.takeover_source
                        ELSE 'ESCALATION'
                    END,
                    reason = %s,
                    updated_at = CURRENT_TIMESTAMP
                    
                RETURNING *
                """,
                (
                    self.account_id,
                    conversation_id,
                    reason,
                    reason,
                ),
            ).fetchone()

        return dict(row)


    async def release(self, conversation_id: str):
        with self._write():
            self.database.conn.execute(
                """
                INSERT INTO conversation_control(
                    account_id,
                    conversation_id,
                    mode,
                    released_at,
                    updated_at
                )
                VALUES (
                    %s, %s, 'AUTO', 
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                
                ON CONFLICT(account_id, conversation_id)
                DO UPDATE SET
                    mode = 'AUTO',
                    operator_id = NULL,
                    takeover_source = NULL,
                    reason = NULL,
                    released_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self.account_id,
                    conversation_id
                ),
            )

        return await self.get(conversation_id)
        