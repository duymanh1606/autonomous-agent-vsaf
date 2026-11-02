"""LangGraph checkpointing on the shared PostgreSQL database.

The checkpoint tables live alongside the agent's own tables. Threads are keyed
by `account_id:task_id` (see `AgentGraph.run_task`), so two accounts resuming
work never address the same checkpoint thread even though they share one
database.

`AsyncPostgresSaver` normally comes from an async context manager. The runtime
needs a saver whose lifetime it controls explicitly, so this owns its
connection and exposes `setup()`/`aclose()` instead.
"""

import logging

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


logger = logging.getLogger(__name__)


class AsyncAgentPostgresSaver(AsyncPostgresSaver):
    """Async checkpointer backed by the shared PostgreSQL database."""

    @classmethod
    async def from_dsn(cls, dsn: str) -> "AsyncAgentPostgresSaver":
        # autocommit is required: `setup()` issues CREATE TABLE / migrations
        # that must not sit inside the runtime's own transaction.
        connection = await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, row_factory=psycopg.rows.dict_row
        )
        saver = cls(connection)
        await saver.setup()
        logger.info("[checkpoint] postgres checkpointer ready")
        return saver

    async def aclose(self) -> None:
        connection = getattr(self, "conn", None)
        if connection is not None and not connection.closed:
            await connection.close()
