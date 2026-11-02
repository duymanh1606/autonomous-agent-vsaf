"""Manually-logged TikTok Shop orders, tracked by the dashboard operator.

Not touched by the agent loop - an operator records an order here after
closing a sale in conversation, so this uses the same lazy self-connecting
PostgreSQL pattern as NotificationStore (dashboard_api/notifications.py)
instead of the agent's own migration-tracked schema in storage/.
"""

from datetime import datetime, timezone
from threading import RLock
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from storage.database import resolve_dsn
from storage.tenancy import TENANT_ROLE, ensure_row_level_security, set_account


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id               BIGSERIAL PRIMARY KEY,
    account_id       TEXT NOT NULL,
    customer_name    TEXT NOT NULL,
    conversation_id  TEXT,
    items            TEXT NOT NULL DEFAULT '',
    total_amount     NUMERIC,
    status           TEXT NOT NULL DEFAULT 'pending',
    shipping_address TEXT NOT NULL DEFAULT '',
    note             TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orders_account_created
    ON orders(account_id, created_at DESC);
-- The products picked from the shop's catalogue, one entry per product:
-- {product_id, name, quantity, unit_price}, name and price as they were when
-- the order was taken. `items` stays the human-readable line built from it
-- (what the order list, search and the sheet export show); an order logged
-- before the picker existed has only `items` and an empty list here.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS line_items JSONB NOT NULL DEFAULT '[]'::jsonb;
-- Whether this order's line_items were taken out of stock when it was
-- placed (dashboard_api/routes/orders.py). False for every order logged
-- before orders touched stock, so cancelling one of those never puts back
-- stock that was never taken.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_deducted BOOLEAN NOT NULL DEFAULT false;
"""

VALID_STATUSES = {"pending", "packing", "shipping", "done", "returned", "cancelled"}

# Order lifecycle: an operator logs the order when the customer confirms it
# (status defaults to "pending"), can still cancel while it is being packed,
# but once it has been handed to the carrier ("shipping") cancellation is no
# longer possible - only the carrier's own outcome (delivered/returned)
# applies. done/returned/cancelled are terminal and cannot be reopened.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"packing", "cancelled"},
    "packing": {"shipping", "cancelled"},
    "shipping": {"done", "returned"},
    "done": set(),
    "returned": set(),
    "cancelled": set(),
}


def _check_transition(old_status: str, new_status: str) -> None:
    if new_status == old_status:
        return
    if new_status not in STATUS_TRANSITIONS.get(old_status, set()):
        raise ValueError(
            f"không thể chuyển trạng thái đơn từ '{old_status}' sang '{new_status}'"
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrderStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = str(dsn or resolve_dsn())
        self._conn = None
        self._lock = RLock()

    def connect(self):
        with self._lock:
            if self._conn is None or self._conn.closed:
                conn = psycopg.connect(
                    self.dsn, row_factory=dict_row, autocommit=True,
                    connect_timeout=5,
                )
                try:
                    # DDL first, as the configured user; then this table is
                    # covered by row-level security and the connection drops
                    # to the tenant role, re-pointed at an account per call.
                    conn.execute(SCHEMA)
                    ensure_row_level_security(conn)
                    conn.execute(f"SET ROLE {TENANT_ROLE}")
                except Exception:
                    conn.close()
                    raise
                self._conn = conn
            return self._conn

    def _for(self, account_id: str):
        """The shared connection, pointed at `account_id`. Call under the lock."""
        conn = self.connect()
        set_account(conn, account_id)
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    def list(self, account_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM orders WHERE account_id=%s ORDER BY created_at DESC"
        params: list[Any] = [account_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._lock:
            rows = self._for(account_id).execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get(self, account_id: str, order_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._for(account_id).execute(
                "SELECT * FROM orders WHERE account_id=%s AND id=%s",
                (account_id, order_id),
            ).fetchone()
        return dict(row) if row else None

    def create(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        status = fields.get("status") or "pending"
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            row = self._for(account_id).execute(
                """INSERT INTO orders
                     (account_id, customer_name, conversation_id, items,
                      line_items, total_amount, status, shipping_address, note,
                      stock_deducted)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (
                    account_id,
                    str(fields.get("customer_name") or "").strip(),
                    fields.get("conversation_id") or None,
                    str(fields.get("items") or "").strip(),
                    Jsonb(list(fields.get("line_items") or [])),
                    fields.get("total_amount"),
                    status,
                    str(fields.get("shipping_address") or "").strip(),
                    str(fields.get("note") or "").strip(),
                    bool(fields.get("stock_deducted")),
                ),
            ).fetchone()
        return dict(row)

    def update(
        self, account_id: str, order_id: int, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        status = fields.get("status") or "pending"
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            current = self.get(account_id, order_id)
            if current is None:
                return None
            _check_transition(current["status"], status)
            row = self._for(account_id).execute(
                """UPDATE orders SET
                     customer_name=%s, conversation_id=%s, items=%s,
                     line_items=%s, total_amount=%s, status=%s,
                     shipping_address=%s, note=%s, updated_at=%s
                   WHERE account_id=%s AND id=%s
                   RETURNING *""",
                (
                    str(fields.get("customer_name") or "").strip(),
                    # Absent = keep. The dashboard's form once never sent it,
                    # and every edit quietly unlinked the order from its
                    # conversation - and so from its row on the Google Sheet.
                    (fields.get("conversation_id") or None)
                    if "conversation_id" in fields else current.get("conversation_id"),
                    str(fields.get("items") or "").strip(),
                    Jsonb(list(fields.get("line_items") or [])),
                    fields.get("total_amount"),
                    status,
                    str(fields.get("shipping_address") or "").strip(),
                    str(fields.get("note") or "").strip(),
                    _now(),
                    account_id,
                    order_id,
                ),
            ).fetchone()
        return dict(row) if row else None

    def set_status(
        self, account_id: str, order_id: int, new_status: str
    ) -> dict[str, Any] | None:
        """Quick status-only transition (e.g. cancel, mark shipped/done) that
        does not touch the rest of the order's fields."""
        if new_status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {new_status}")
        with self._lock:
            current = self.get(account_id, order_id)
            if current is None:
                return None
            _check_transition(current["status"], new_status)
            row = self._for(account_id).execute(
                """UPDATE orders SET status=%s, updated_at=%s
                   WHERE account_id=%s AND id=%s
                   RETURNING *""",
                (new_status, _now(), account_id, order_id),
            ).fetchone()
        return dict(row)

    def delete(self, account_id: str, order_id: int) -> bool:
        with self._lock:
            cursor = self._for(account_id).execute(
                "DELETE FROM orders WHERE account_id=%s AND id=%s",
                (account_id, order_id),
            )
            return cursor.rowcount > 0
