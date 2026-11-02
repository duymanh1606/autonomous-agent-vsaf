"""Bounded, account-scoped retention jobs with dry-run as the default."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from models.account import require_account_id


@dataclass(frozen=True)
class RetentionReport:
    account_id: str
    dry_run: bool
    actions: int
    tasks: int
    notifications: int


class DatabaseMaintenance:
    def __init__(self, database, *, account_id=None, batch_size=1000):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database
        self.batch_size = max(1, min(10_000, int(batch_size)))

    def _count(self, table, predicate, params):
        row = self.database.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE account_id = %s AND {predicate}",
            (self.account_id, *params),
        ).fetchone()
        return int(row["count"])

    def _delete_batch(self, table, predicate, params):
        cursor = self.database.conn.execute(
            f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} "
            f"WHERE account_id = %s AND {predicate} LIMIT %s)",
            (self.account_id, *params, self.batch_size),
        )
        return cursor.rowcount

    def run(self, *, older_than_days=30, dry_run=True):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(older_than_days)))
        ).isoformat()
        rules = {
            "actions": ("created_at < %s", (cutoff,)),
            "tasks": (
                "updated_at < %s AND status IN ('completed', 'failed') "
                "AND NOT EXISTS (SELECT 1 FROM message_events e WHERE "
                "e.account_id = tasks.account_id AND e.reply_task_id = tasks.id)",
                (cutoff,),
            ),
            "notifications": (
                "created_at < %s AND status = 'RESOLVED'", (cutoff,)
            ),
        }
        counts = {
            table: self._count(table, predicate, params)
            for table, (predicate, params) in rules.items()
        }
        if not dry_run:
            with self.database.conn.transaction():
                for table, (predicate, params) in rules.items():
                    remaining = counts[table]
                    while remaining > 0:
                        deleted = self._delete_batch(table, predicate, params)
                        if deleted <= 0:
                            break
                        remaining -= deleted
        return RetentionReport(self.account_id, bool(dry_run), **counts)
