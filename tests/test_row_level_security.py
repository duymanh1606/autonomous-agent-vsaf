"""Row-level security: the database keeps each shop to its own rows.

Every other isolation test proves that the queries the code *does* run carry
`account_id`. These prove the layer under them: a query that forgets the
filter - the defect storage/audit.py exists to catch, and cannot catch when
SQL is built dynamically - still returns one shop's rows only, and a write
aimed at another shop is refused by PostgreSQL itself.
"""

import sys
import unittest
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.db_reader import AgentDatabaseReader
from dashboard_api.orders import OrderStore
from dashboard_api.translations import TranslationStore
from storage.database import Database, SecondaryConnection
from storage.tenancy import (
    POLICY_NAME, TENANT_ROLE, UNSCOPED_TABLES, ensure_row_level_security,
    scope_to_account, set_account,
)
from tests.support import SCHEMA_PATH, make_database, unscoped_connection


class RowLevelSecurityTests(unittest.TestCase):
    def setUp(self):
        self.a = make_database(self, account_id="shop-a")
        self.admin = unscoped_connection(self, self.a)
        # Shop B as its own process: its own Database on the same schema.
        self.b = Database(self.a.dsn, SCHEMA_PATH, account_id="shop-b")
        self.addCleanup(self.b.close)
        for database, task_id in ((self.a, "task-a"), (self.b, "task-b")):
            database.conn.execute(
                "INSERT INTO tasks(account_id, id, capability, platform, status) "
                "VALUES (%s, %s, 'scan', 'tiktok_web', 'pending')",
                (database.account_id, task_id),
            )
            database.conn.commit()

    def ids(self, conn, sql="SELECT id FROM tasks ORDER BY id /* rls-probe */"):
        return [row["id"] for row in conn.execute(sql).fetchall()]

    def test_a_query_without_its_account_filter_sees_one_shop_only(self):
        self.assertEqual(self.ids(self.a.conn), ["task-a"])
        self.assertEqual(self.ids(self.b.conn), ["task-b"])
        # The unscoped connection - backups, migrations - still sees both.
        self.assertEqual(self.ids(self.admin), ["task-a", "task-b"])

    def test_writing_a_row_for_another_shop_is_refused(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.a.conn.execute(
                "INSERT INTO tasks(account_id, id, capability, platform, status) "
                "VALUES ('shop-b', 'forged', 'scan', 'tiktok_web', 'pending')"
            )
        self.a.conn.rollback()

    def test_moving_a_row_to_another_shop_is_refused(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.a.conn.execute("UPDATE tasks SET account_id = 'shop-b' WHERE id = 'task-a'")
        self.a.conn.rollback()

    def test_another_shops_rows_cannot_be_changed_or_deleted(self):
        updated = self.a.conn.execute("UPDATE tasks SET status = 'failed' /* rls-probe */").rowcount
        deleted = self.a.conn.execute("DELETE FROM tasks WHERE id = 'task-b' /* rls-probe */").rowcount
        self.a.conn.commit()
        self.assertEqual((updated, deleted), (1, 0))
        row = self.admin.execute("SELECT status FROM tasks WHERE id = 'task-b' /* rls-probe */").fetchone()
        self.assertEqual(row["status"], "pending")

    def test_a_rolled_back_transaction_does_not_undo_the_scope(self):
        """A plain SET inside a transaction is reverted by its rollback; the
        scope is committed on its own so this cannot hand the connection back
        to the unrestricted user."""
        self.a.conn.execute("SELECT 1")
        self.a.conn.rollback()
        row = self.a.conn.execute(
            "SELECT current_user AS role, current_setting('app.account_id', true) AS account"
        ).fetchone()
        self.assertEqual((row["role"], row["account"]), (TENANT_ROLE, "shop-a"))
        self.assertEqual(self.ids(self.a.conn), ["task-a"])

    def test_a_tenant_connection_with_no_account_sees_nothing(self):
        """Fail-closed: forgetting to name the account is an empty result,
        never every shop's rows."""
        with psycopg.connect(self.a.dsn, row_factory=dict_row, autocommit=True) as conn:
            conn.execute(f"SET ROLE {TENANT_ROLE}")
            self.assertEqual(self.ids(conn), [])

    def test_set_account_refuses_an_unscoped_connection(self):
        with self.assertRaises(RuntimeError):
            set_account(self.admin, "shop-a")

    def test_scoping_refuses_a_connection_mid_transaction(self):
        with psycopg.connect(self.a.dsn, row_factory=dict_row) as conn:
            conn.execute("SELECT 1")
            with self.assertRaises(RuntimeError):
                scope_to_account(conn, "shop-a")

    def test_every_account_table_is_protected_or_deliberately_shared(self):
        """A new table with an account_id column is covered at the next start;
        this fails if one ever slips through without a policy."""
        rows = self.admin.execute(
            """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                      EXISTS (SELECT 1 FROM pg_policies p
                               WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                                 AND p.policyname = %s) AS has_policy
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'account_id'
                WHERE n.nspname = current_schema() AND c.relkind = 'r'""",
            (POLICY_NAME,),
        ).fetchall()
        self.assertGreater(len(rows), 10)
        for row in rows:
            with self.subTest(table=row["relname"]):
                if row["relname"] in UNSCOPED_TABLES:
                    continue
                self.assertTrue(row["relrowsecurity"] and row["relforcerowsecurity"])
                self.assertTrue(row["has_policy"])

    def test_a_table_added_later_is_covered_on_the_next_start(self):
        self.admin.execute("CREATE TABLE added_later (account_id TEXT NOT NULL, note TEXT)")
        self.admin.execute("INSERT INTO added_later VALUES ('shop-a', 'a'), ('shop-b', 'b')")
        ensure_row_level_security(self.admin)
        ensure_row_level_security(self.admin)  # idempotent
        self.assertEqual(
            [r["note"] for r in self.a.conn.execute("SELECT note FROM added_later").fetchall()],
            ["a"],
        )


class ScopedReadersTests(unittest.TestCase):
    """The supervisor's per-workspace connections and shared stores."""

    def setUp(self):
        self.database = make_database(self, account_id="shop-a")
        self.admin = unscoped_connection(self, self.database)
        self.admin.execute(
            "INSERT INTO accounts(id, platform) VALUES ('shop-b', 'tiktok_web') "
            "ON CONFLICT DO NOTHING"
        )
        self.admin.execute(
            "INSERT INTO tasks(account_id, id, capability, platform, status) VALUES "
            "('shop-a', 'task-a', 'scan', 'tiktok_web', 'pending'), "
            "('shop-b', 'task-b', 'scan', 'tiktok_web', 'pending')"
        )

    def test_secondary_connection_is_scoped(self):
        secondary = SecondaryConnection(self.database.dsn, account_id="shop-b")
        self.addCleanup(secondary.close)
        rows = secondary.conn.execute("SELECT id FROM tasks /* rls-probe */").fetchall()
        self.assertEqual([row["id"] for row in rows], ["task-b"])

    def test_dashboard_reader_is_scoped(self):
        reader = AgentDatabaseReader(self.database.dsn, account_id="shop-b")
        self.addCleanup(reader.close)
        rows = reader.connect().execute("SELECT id FROM tasks /* rls-probe */").fetchall()
        self.assertEqual([row["id"] for row in rows], ["task-b"])

    def test_the_shared_order_store_switches_account_per_call(self):
        store = OrderStore(self.database.dsn)
        self.addCleanup(store.close)
        fields = {"customer_name": "Lan", "phone": "0900000000", "address": "HN"}
        store.create("shop-a", dict(fields))
        store.create("shop-b", {**fields, "customer_name": "Minh"})

        self.assertEqual([o["customer_name"] for o in store.list("shop-a")], ["Lan"])
        self.assertEqual([o["customer_name"] for o in store.list("shop-b")], ["Minh"])
        # Even an unfiltered read on its connection sees the current account only.
        conn = store._for("shop-a")
        self.assertEqual(len(conn.execute("SELECT id FROM orders /* rls-probe */").fetchall()), 1)

    def test_the_shared_translation_store_is_scoped(self):
        store = TranslationStore(self.database.dsn)
        self.addCleanup(store.close)
        store.put_many("shop-a", {"h1": "xin chào"})
        store.put_many("shop-b", {"h1": "chào bạn"})
        self.assertEqual(store.get_many("shop-a", ["h1"]), {"h1": "xin chào"})
        self.assertEqual(store.get_many("shop-b", ["h1"]), {"h1": "chào bạn"})


if __name__ == "__main__":
    unittest.main()
