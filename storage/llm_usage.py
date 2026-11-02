"""Gemini token usage, attributed to a workspace (`account_id`).

Written from deep inside `llm/gemini_flash_lite.py` and `ai/client.py` -
both run as part of the agent process, not `dashboard_api`, so this uses the
same lazy self-connecting PostgreSQL pattern as `dashboard_api/orders.py`
rather than the agent's migration-tracked schema, to avoid coupling a
metering side-effect to `Database.initialize()`. Read back by
`dashboard_api/main.py` to show an admin how many tokens each dashboard
login's workspaces have consumed.
"""

import logging
from threading import RLock
from typing import Any

import psycopg
from psycopg.rows import dict_row

from storage.database import resolve_dsn


logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                BIGSERIAL PRIMARY KEY,
    account_id        TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_account_created
    ON llm_usage(account_id, created_at DESC);
"""


class LlmUsageStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = str(dsn or resolve_dsn())
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._lock = RLock()

    def connect(self):
        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(
                    self.dsn, row_factory=dict_row, autocommit=True,
                    connect_timeout=5,
                )
                self._conn.execute(SCHEMA)
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    def record(
        self, *, account_id: str, provider: str, model: str,
        prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        with self._lock:
            self.connect().execute(
                """INSERT INTO llm_usage
                     (account_id, provider, model, prompt_tokens,
                      completion_tokens, total_tokens)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (account_id, provider, model, prompt_tokens,
                 completion_tokens, total_tokens),
            )

    def totals_by_account(self, account_ids: list[str]) -> dict[str, dict[str, int]]:
        if not account_ids:
            return {}
        with self._lock:
            rows = self.connect().execute(
                """SELECT account_id,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COUNT(*) AS calls
                   FROM llm_usage WHERE account_id = ANY(%s)
                   GROUP BY account_id""",
                (account_ids,),
            ).fetchall()
        return {
            row["account_id"]: {
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "calls": int(row["calls"]),
            }
            for row in rows
        }


_usage_store: LlmUsageStore | None = None


def record_usage(
    account_id: str | None, provider: str, model: str, meta: dict[str, Any] | None,
) -> None:
    """Best-effort usage capture - a metering hiccup must never break an LLM
    call, so every failure here is swallowed and logged, not raised."""
    if not account_id or not meta:
        return
    global _usage_store
    try:
        if _usage_store is None:
            _usage_store = LlmUsageStore()
        _usage_store.record(
            account_id=account_id, provider=provider, model=model,
            prompt_tokens=int(meta.get("promptTokenCount") or 0),
            completion_tokens=int(meta.get("candidatesTokenCount") or 0),
            total_tokens=int(meta.get("totalTokenCount") or 0),
        )
    except Exception as exc:
        logger.warning(
            "[llm_usage] failed to record usage: %s", type(exc).__name__, exc_info=True,
        )
