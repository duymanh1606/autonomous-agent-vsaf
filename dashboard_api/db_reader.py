"""Read-only access to one account's slice of the shared agent database.

This deliberately does NOT reuse `storage.Database` or the repositories. Those
carry destructive startup behaviour - `requeue_interrupted_tasks()` resets every
`running` task to `pending`, `expire_interrupted_send_tasks()` expires every
in-flight send reservation, and `PostgresProcessedMessageStore.pending()` mutates
rows before selecting them. Constructing them here would corrupt the state of a
perfectly healthy agent just because someone opened the dashboard.

Every statement goes through `_query`, which refuses anything that is not a
SELECT. That is the enforcement, not a convention.
"""

import json
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from models.account import require_account_id
from storage.tenancy import scope_to_account
from storage.database import resolve_dsn


logger = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    pass


class AgentDatabaseReader:
    """One read-only view of one account's rows in the shared database.

    Every account's data lives in the same PostgreSQL database, so the account
    filter is the *only* thing separating one dashboard from another. Every
    query below therefore carries `account_id`; there is no path that reads a
    table without it.

    The session is additionally set read-only at the transaction level, so a
    mistake here cannot write to a live agent's rows.
    """

    def __init__(self, dsn: str | None = None, *, account_id: str, config=None):
        self.account_id = require_account_id(account_id)
        self.dsn = str(dsn) if dsn else resolve_dsn(config)
        self._conn = None

    @property
    def exists(self) -> bool:
        return self.connect() is not None

    def connect(self):
        if self._conn is None or getattr(self._conn, "closed", True):
            try:
                self._conn = psycopg.connect(
                    self.dsn, row_factory=dict_row, connect_timeout=5,
                    autocommit=True,
                )
                # Belt and braces with `_query`: even a statement that slipped
                # past the SELECT check cannot modify a live agent's rows.
                self._conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                # And the database itself keeps it to this workspace's rows.
                scope_to_account(self._conn, self.account_id)
            except psycopg.Error as exc:
                logger.warning("[reader] cannot connect: %s", exc)
                self._conn = None
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    # -------------------------------------------------------------- core
    def _query(self, sql: str, params=()) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise ReadOnlyViolation(f"refusing non-SELECT statement: {sql[:40]}")
        conn = self.connect()
        if conn is None:
            return []
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        except psycopg.Error as exc:
            logger.warning(
                "[reader] query failed account=%s error=%s", self.account_id, exc
            )
            return []

    @staticmethod
    def _json(value, default=None):
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------- reads
    def health(self) -> dict[str, Any]:
        if self.connect() is None:
            return {"ok": False, "detail": "database unreachable"}
        rows = self._query("SELECT 1 AS ok")
        return {"ok": bool(rows), "detail": f"account={self.account_id}"}

    def counts(self) -> dict[str, int]:
        sql = """
            SELECT
              (SELECT COUNT(*) FROM tasks WHERE account_id=%(account)s)                    AS tasks,
              (SELECT COUNT(*) FROM tasks WHERE account_id=%(account)s AND status='pending')   AS tasks_pending,
              (SELECT COUNT(*) FROM tasks WHERE account_id=%(account)s AND status='running')   AS tasks_running,
              (SELECT COUNT(*) FROM tasks WHERE account_id=%(account)s AND status='failed')    AS tasks_failed,
              (SELECT COUNT(*) FROM tasks WHERE account_id=%(account)s AND status='completed') AS tasks_completed,
              (SELECT COUNT(*) FROM customer_states WHERE account_id=%(account)s)          AS customers,
              (SELECT COUNT(*) FROM conversation_messages WHERE account_id=%(account)s)    AS messages,
              (SELECT COUNT(*) FROM message_escalations
                 WHERE account_id=%(account)s AND status='OPEN')                           AS escalations_open
        """
        rows = self._query(sql, {"account": self.account_id})
        return rows[0] if rows else {}

    def tasks(self, *, status: str | None = None, capability: str | None = None,
              limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        # The account filter is part of the literal, never an optional clause,
        # so it cannot be dropped by a future edit to the filter list.
        clauses, params = [], [self.account_id]
        if status:
            clauses.append("status = %s")
            params.append(status)
        if capability:
            clauses.append("capability = %s")
            params.append(capability)
        extra = "".join(f" AND {clause}" for clause in clauses)
        rows = self._query(
            f"""SELECT id, capability, platform, priority, status, error,
                       send_status, send_attempt_count, created_at
                FROM tasks WHERE account_id = %s{extra}
                ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (*params, int(limit), int(offset)),
        )
        return rows

    def task(self, task_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM tasks WHERE account_id = %s AND id = %s",
            (self.account_id, task_id),
        )
        if not rows:
            return None
        task = rows[0]
        task["payload"] = self._json(task.get("payload"), {})
        task["result"] = self._json(task.get("result"), {})
        task["actions"] = self.actions(task_id)
        return task

    def actions(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT id, action_name, args, success, error, created_at
               FROM actions WHERE account_id = %s AND task_id = %s
               ORDER BY id ASC LIMIT %s""",
            (self.account_id, task_id, int(limit)),
        )
        for row in rows:
            row["args"] = self._json(row.get("args"), {})
        return rows

    def activity(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest actions across every task - the live activity feed."""
        rows = self._query(
            """SELECT a.id, a.task_id, a.action_name, a.success, a.error,
                      a.created_at, t.capability
               FROM actions a
               LEFT JOIN tasks t ON t.id = a.task_id AND t.account_id = a.account_id
               WHERE a.account_id = %s
               ORDER BY a.id DESC LIMIT %s""",
            (self.account_id, int(limit)),
        )
        return rows

    def escalations(self, *, status: str = "OPEN", limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT id, event_id, conversation_id, customer_id, reason,
                      status, created_at, state_json, analysis_json
               FROM message_escalations
               WHERE account_id = %s AND status = %s ORDER BY id DESC LIMIT %s""",
            (self.account_id, status, int(limit)),
        )
        for row in rows:
            row["state"] = self._json(row.pop("state_json", None), {})
            row["analysis"] = self._json(row.pop("analysis_json", None), {})
        return rows

    def conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        """Customer list: one row per conversation, newest activity first."""
        rows = self._query(
            """SELECT m.conversation_id,
                      MAX(m.created_at)                      AS last_message_at,
                      COUNT(*)                               AS message_count,
                      SUM(CASE WHEN m.direction='INBOUND'  THEN 1 ELSE 0 END) AS inbound,
                      SUM(CASE WHEN m.direction='OUTBOUND' THEN 1 ELSE 0 END) AS outbound,
                      p.username, p.display_name, p.avatar_url, p.phone,
                      s.customer_id, s.state_json,
                      cc.mode, cc.operator_id, cc.takeover_source, cc.reason
               FROM conversation_messages m
               LEFT JOIN customer_profile p
                      ON p.conversation_id = m.conversation_id
                     AND p.account_id = m.account_id
               LEFT JOIN customer_states  s
                      ON s.conversation_id = m.conversation_id
                     AND s.account_id = m.account_id
               LEFT JOIN conversation_control cc
                      ON cc.conversation_id = m.conversation_id
                     AND cc.account_id = m.account_id
               WHERE m.account_id = %s
               GROUP BY m.conversation_id, p.username, p.display_name, p.avatar_url, p.phone,
                        s.customer_id, s.state_json,
                        cc.mode, cc.operator_id, cc.takeover_source, cc.reason
               ORDER BY last_message_at DESC LIMIT %s""",
            (self.account_id, int(limit)),
        )
        for row in rows:
            state = self._json(row.pop("state_json", None), {}) or {}
            row["intent"] = state.get("intent")
            row["stage"] = state.get("stage")
            row["requires_human"] = state.get("requires_human")
            row["summary"] = state.get("conversation_summary")
            row["control"] = self._control({
                "mode": row.pop("mode", None),
                "operator_id": row.pop("operator_id", None),
                "takeover_source": row.pop("takeover_source", None),
                "reason": row.pop("reason", None),
            })
        return rows

    def customer_identity(self, conversation_id: str | None) -> dict[str, Any]:
        """username for one conversation - used to enrich the Google Sheet
        export (integrations/sheet_export.py) with the customer's TikTok
        identity when logging an order. `{}` (no `conversation_id`, or no
        profile row yet) means the sheet gets a blank cell, not a dropped
        export."""
        if not conversation_id:
            return {}
        rows = self._query(
            "SELECT username FROM customer_profile "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        )
        return rows[0] if rows else {}

    def recent_messages(self, limit: int = 1500) -> list[dict[str, Any]]:
        """Newest messages across every conversation - the candidate set the
        global search (dashboard_api/search.py) matches message text against.
        Bounded on purpose: accent-insensitive matching happens in Python,
        so this is "search recent history", not a full-text index."""
        return self._query(
            """SELECT conversation_id, direction, text, created_at
               FROM conversation_messages
               WHERE account_id = %s
               ORDER BY created_at DESC LIMIT %s""",
            (self.account_id, int(limit)),
        )

    def conversation(self, conversation_id: str, limit: int = 200) -> dict[str, Any]:
        # `limit` has to cut from the *old* end, not the new one. Ordering
        # ascending and taking the first N looks equivalent until a thread
        # passes N messages, at which point every newer message falls outside
        # the window permanently - the conversation freezes on screen while
        # the customer keeps writing, and nothing about it looks broken:
        # the query succeeds, the API returns 200, the rows are all real.
        # Confirmed live on a 241-message thread whose last 41 messages had
        # stopped appearing. So take the newest N, then flip them back into
        # reading order.
        messages = self._query(
            """SELECT * FROM (
                 SELECT message_id, direction, text, created_at, source,
                        media_json, id
                   FROM conversation_messages
                  WHERE account_id = %s AND conversation_id = %s
               ORDER BY created_at DESC, id DESC
                  LIMIT %s
               ) AS recent
               ORDER BY created_at ASC, id ASC""",
            (self.account_id, conversation_id, int(limit)),
        )
        for row in messages:
            row.pop("id", None)
        for row in messages:
            row["media"] = self._json(row.pop("media_json", None), [])
        profile = self._query(
            "SELECT * FROM customer_profile "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        )
        facts = self._query(
            """SELECT fact_type, fact_value, confidence, source_message_id, updated_at
               FROM customer_facts WHERE account_id = %s AND conversation_id = %s
               ORDER BY fact_type""",
            (self.account_id, conversation_id),
        )
        state_rows = self._query(
            "SELECT state_json, customer_id, updated_at FROM customer_states "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        )
        state = self._json(state_rows[0]["state_json"], {}) if state_rows else {}
        control_rows = self._query(
            """SELECT mode, operator_id, takeover_source, reason
               FROM conversation_control
               WHERE account_id = %s AND conversation_id = %s""",
            (self.account_id, conversation_id),
        )
        return {
            "conversation_id": conversation_id,
            "profile": profile[0] if profile else None,
            "facts": facts,
            "state": state,
            "messages": messages,
            "control": self._control(control_rows[0] if control_rows else {}),
        }

    @staticmethod
    def _control(row: dict[str, Any]) -> dict[str, Any]:
        """Default AUTO/None control state - absence of a row means AUTO."""
        return {
            "mode": row.get("mode") or "AUTO",
            "operator_id": row.get("operator_id"),
            "takeover_source": row.get("takeover_source"),
            "reason": row.get("reason"),
        }
