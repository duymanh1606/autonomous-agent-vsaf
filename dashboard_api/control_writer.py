"""The one write the supervisor is allowed to make into the agent database.

`AgentDatabaseReader` next door is read-only on purpose, and that is the right
default: the supervisor must never be able to corrupt a healthy agent's rows.
This module is the single, named exception, and it exists because the previous
arrangement had a hole an operator could fall into.

`conversation_control` could only be written through the control server, which
runs *inside* the agent process. So a workspace that was stopped - crashed,
deliberately stopped, or waiting to be restarted after a deploy - could not be
taken over at all: `POST .../takeover` answered 503 and the conversation stayed
`WAITING_HUMAN` indefinitely, even though PostgreSQL was up and the supervisor
already held its DSN. A customer waiting for a person was unreachable for
exactly the reason a person was needed.

The control plane must not die with the data plane, so when the agent does not
answer, the supervisor writes the row itself. Three things keep that narrow:

* the agent is always asked first, and this runs only after it failed to
  answer - never as the primary path, so the agent keeps publishing its own
  events and keeps its own view in step;
* the write reuses `ConversationControlRepository`, so TTL settlement and the
  `mode <> 'HUMAN'` guard are the same code the agent runs, not a second
  implementation of the same rules;
* nothing else is writable from here. This is takeover and release, on one
  table, for one account.

The agent picks the change up on its own: every mode check reads the row fresh
from PostgreSQL (`core/policy.py`'s send gate and the inbox scan's two
`agent_may_reply` calls), so there is nothing to notify and no cache to
invalidate.
"""

import logging

from models.account import require_account_id
from storage.conversation_control import ConversationControlRepository
from storage.database import SecondaryConnection


logger = logging.getLogger(__name__)


class ConversationControlWriter:
    """Takeover and release for one account, without going through the agent."""

    def __init__(self, dsn: str | None = None, *, account_id: str, config=None,
                 human_takeover_ttl_minutes=60, waiting_human_ttl_minutes=20):
        self.account_id = require_account_id(account_id)
        # The workspace's own TTLs, as its agent reads them - with the
        # repository defaults instead, the two sides could disagree about
        # whether a takeover had lapsed.
        self.ttls = {
            "human_takeover_ttl_minutes": human_takeover_ttl_minutes,
            "waiting_human_ttl_minutes": waiting_human_ttl_minutes,
        }
        # `SecondaryConnection` is autocommit, so the repository's own
        # `with conn.transaction()` is what groups each write - there is no
        # half-written row waiting on a commit this class forgot to make.
        self.database = SecondaryConnection(
            dsn, account_id=self.account_id, config=config
        )

    def _repository(self):
        return ConversationControlRepository(
            self.database, account_id=self.account_id, **self.ttls
        )

    async def takeover(self, conversation_id: str, operator_id: str) -> dict:
        row = await self._repository().takeover(conversation_id, operator_id)
        logger.warning(
            "[control] agent unreachable; wrote takeover directly account=%s "
            "conversation=%s operator=%s mode=%s",
            self.account_id, conversation_id, operator_id,
            (row or {}).get("mode", "unknown"),
        )
        return row

    async def release(self, conversation_id: str) -> dict:
        row = await self._repository().release(conversation_id)
        logger.warning(
            "[control] agent unreachable; wrote release directly account=%s "
            "conversation=%s mode=%s",
            self.account_id, conversation_id, (row or {}).get("mode", "unknown"),
        )
        return row

    async def renew_human(self, conversation_id: str) -> dict:
        """Keep an operator's takeover alive while they are sending.

        Unlike takeover and release this is always written here, not asked of
        the agent first: it announces nothing, and a send must renew the claim
        whether or not the agent is answering right now.
        """
        return await self._repository().renew_human(conversation_id)

    def close(self) -> None:
        self.database.close()
