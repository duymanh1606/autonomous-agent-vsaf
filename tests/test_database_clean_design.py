import unittest
from datetime import datetime, timezone
from pathlib import Path

from memory.processed_messages import PostgresProcessedMessageStore
from models.message_event import MessageEvent
from storage.database import Database, SchemaMigrationError
from models.tasks import DEFAULT_TASK_PRIORITY
from storage.repositories import TaskRepository
from tests.support import make_database, unscoped_connection


ROOT = Path(__file__).resolve().parents[1]


class CleanSchemaStaticTests(unittest.TestCase):
    def test_baseline_contains_scoped_keys_hot_indexes_and_no_screen_memory(self):
        schema = (ROOT / "storage" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("PRIMARY KEY (account_id, id)", schema)
        self.assertIn("USING BRIN(created_at)", schema)
        self.assertIn("WHERE status IN ('DETECTED', 'PROCESSING')", schema)
        self.assertIn("screen_transition_stats", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS screen_memory", schema)

    def test_applied_migration_checksum_mismatch_stops_startup(self):
        with self.assertRaises(SchemaMigrationError):
            Database._verify_checksum("v1", "new", {"checksum": "old"})


class CleanSchemaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)

    async def test_task_identity_is_scoped_to_account(self):
        admin = unscoped_connection(self, self.database)
        admin.execute(
            "INSERT INTO accounts(id, platform) VALUES ('other', 'tiktok_web')"
        )
        for account_id in ("acc_test", "other"):
            admin.execute(
                "INSERT INTO tasks(account_id, id, capability, platform, status) "
                "VALUES (%s, 'same-id', 'scan', 'tiktok_web', 'pending')",
                (account_id,),
            )
        row = admin.execute(
            "SELECT COUNT(*) AS count FROM tasks WHERE id = 'same-id' "
            "AND account_id IN ('acc_test', 'other')"
        ).fetchone()
        self.assertEqual(row["count"], 2)

    async def test_queued_priority_work_is_visible_to_a_running_task(self):
        """`has_pending_above_priority` is what lets an inbox scan notice it is
        standing in a staff reply's way: the agent owns one Chrome session, and
        a staff reply is submitted at priority 100 so it is claimed the moment
        the current task ends - which is no help while a hold is running."""
        repository = TaskRepository(self.database)
        routine = DEFAULT_TASK_PRIORITY

        self.assertFalse(await repository.has_pending_above_priority(routine))

        self.database.conn.execute(
            "INSERT INTO tasks(account_id, id, capability, platform, status, priority) "
            "VALUES ('acc_test', 'scan-1', 'scan_inbox', 'tiktok_web', 'pending', %s)",
            (routine,),
        )
        self.database.conn.commit()
        # Another routine task is not a reason to give up the session.
        self.assertFalse(await repository.has_pending_above_priority(routine))

        self.database.conn.execute(
            "INSERT INTO tasks(account_id, id, capability, platform, status, priority) "
            "VALUES ('acc_test', 'human-1', 'send_message', 'tiktok_web', 'pending', 100)"
        )
        self.database.conn.commit()
        self.assertTrue(await repository.has_pending_above_priority(routine))

        # Already running is not waiting: only a pending task is being held up.
        # (`status <> 'running' OR owner_id IS NOT NULL` is a schema check, so
        # a running task has to name its owner.)
        self.database.conn.execute(
            "UPDATE tasks SET status = 'running', owner_id = 'agent-1' "
            "WHERE account_id = 'acc_test' AND id = 'human-1'"
        )
        self.database.conn.commit()
        self.assertFalse(await repository.has_pending_above_priority(routine))

    async def test_another_account_priority_work_is_not_visible(self):
        """One busy workspace must never hand its Chrome session over because
        a different shop has a staff reply waiting."""
        repository = TaskRepository(self.database)
        other_shop = unscoped_connection(self, self.database)
        other_shop.execute(
            "INSERT INTO accounts(id, platform) VALUES ('other', 'tiktok_web')"
        )
        other_shop.execute(
            "INSERT INTO tasks(account_id, id, capability, platform, status, priority) "
            "VALUES ('other', 'human-2', 'send_message', 'tiktok_web', 'pending', 100)"
        )

        self.assertFalse(
            await repository.has_pending_above_priority(DEFAULT_TASK_PRIORITY)
        )

    async def test_live_event_lease_is_not_reset_but_expired_one_is_recovered(self):
        store = PostgresProcessedMessageStore(
            self.database, owner_id="worker-a", lease_seconds=60
        )
        event = MessageEvent(
            id="event-1", account_id="acc_test", platform="tiktok_web",
            conversation_id="customer", customer_id="customer", message_id="m1",
            message="hello", timestamp=datetime.now(timezone.utc),
        )
        self.assertTrue(await store.claim(event))
        self.assertEqual(await store.pending(), [])
        self.database.conn.execute(
            "UPDATE message_events SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
            "WHERE account_id = 'acc_test' AND event_id = 'event-1'"
        )
        self.database.conn.commit()
        self.assertEqual([item.id for item in await store.pending()], ["event-1"])

    async def test_screen_transition_observations_are_upserted(self):
        repository = TaskRepository(self.database)
        await repository.record_transition("HOME", "open_inbox", "INBOX", 0.8)
        await repository.record_transition("HOME", "open_inbox", "INBOX", 1.0)
        row = self.database.conn.execute(
            "SELECT observation_count, average_confidence FROM screen_transition_stats "
            "WHERE account_id = 'acc_test'"
        ).fetchone()
        self.assertEqual(row["observation_count"], 2)
        self.assertAlmostEqual(row["average_confidence"], 0.9)
