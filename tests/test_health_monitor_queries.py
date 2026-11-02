"""The monitor's SQL, run against the real schema.

`test_health_monitor.py` stubs `_query` to test what the checks *decide*.
That leaves the queries themselves unproven - and a monitor whose SQL is
wrong fails in the worst possible way: silently, reporting healthy. These run
the real statements against real tables, so a renamed column breaks a test
instead of turning the alerting off.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.health_monitor import (
    CUSTOMER_WAIT_LOOKBACK_HOURS,
    CUSTOMER_WAIT_MINUTES,
    HealthMonitor,
    STUCK_ATTEMPTS,
)
from tests.support import make_database, unscoped_connection


class StubNotifications:
    def __init__(self):
        self.opened, self.resolved = [], []

    def record(self, **kwargs):
        self.opened.append(kwargs)

    def resolve_kind(self, account_id, kind, dedup_key):
        self.resolved.append(dedup_key)


class HealthMonitorSqlTests(unittest.TestCase):
    def setUp(self):
        self.database = make_database(self, account_id="acct-1")
        self.notifications = StubNotifications()
        self.monitor = HealthMonitor(
            self.notifications,
            database_url_resolver=lambda: self.database.dsn,
            list_accounts=lambda: [SimpleNamespace(id="acct-1")],
            backup_dir=Path("/nonexistent"),
        )
        self.database.conn.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acct-1', 'tiktok_web') "
            "ON CONFLICT DO NOTHING"
        )
        self.database.conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id) "
            "VALUES ('acct-1', 'conv') ON CONFLICT DO NOTHING"
        )
        self.database.conn.commit()

    def _ledger(self, name, attempts, state="UNANSWERED"):
        self.database.conn.execute(
            """INSERT INTO inbox_ledger
                 (account_id, conversation_key, conversation_name, state,
                  status, attempts, updated_at)
               VALUES ('acct-1', %s, %s, %s, 'no_inbound_message', %s, now())""",
            (name, name, state, attempts),
        )
        self.database.conn.commit()

    def _task(self, task_id, status):
        self.database.conn.execute(
            """INSERT INTO tasks
                 (account_id, id, capability, platform, priority, payload,
                  status, created_at, updated_at)
               VALUES ('acct-1', %s, 'messaging.scan_inbox', 'tiktok_web',
                       5, '{}', %s, now(), now())""",
            (task_id, status),
        )
        self.database.conn.commit()

    def _escalation(self, event_id, status="OPEN"):
        # An escalation is always about a real message event - the schema
        # enforces it, so the fixture has to build the same shape production
        # does rather than a bare row.
        self.database.conn.execute(
            """INSERT INTO message_events
                 (account_id, event_id, dedup_key, platform, conversation_id,
                  customer_id, message_id, event_type, direction, status,
                  detected_at, event_json)
               VALUES ('acct-1', %s, %s, 'tiktok_web', 'conv', 'cust', %s,
                       'RECEIVED', 'INBOUND', 'NEEDS_REPLY', now(), '{}')""",
            (event_id, event_id, event_id),
        )
        self.database.conn.execute(
            """INSERT INTO message_escalations
                 (account_id, event_id, conversation_id, customer_id, reason,
                  state_json, analysis_json, status, created_at, updated_at)
               VALUES ('acct-1', %s, 'conv', 'cust', 'requires_human',
                       '{}', '{}', %s, now(), now())""",
            (event_id, status),
        )
        self.database.conn.commit()

    # --------------------------------------------------- customers waiting
    def _message(self, direction, minutes_ago, conversation="conv"):
        """Stored in call order, like the agent stores what it reads."""
        self.database.conn.execute(
            """INSERT INTO conversation_messages
                 (account_id, message_id, conversation_id, customer_id,
                  direction, text, created_at)
               VALUES ('acct-1', %s, %s, 'cust', %s, 'x',
                       now() - make_interval(mins => %s::int))""",
            (f"m-{self._next_id()}", conversation, direction, minutes_ago),
        )
        self.database.conn.commit()

    def _next_id(self):
        self._ids = getattr(self, "_ids", 0) + 1
        return self._ids

    def test_a_customer_waiting_past_the_threshold_alerts(self):
        self._message("OUTBOUND", 60)
        self._message("INBOUND", CUSTOMER_WAIT_MINUTES + 10)
        self._message("INBOUND", 2)  # a burst: waiting counts from the first

        problem = self.monitor.check_customers_waiting("acct-1")

        self.assertIsNotNone(problem)
        self.assertIn("1 khách", problem.title)
        self.assertIn(f"{CUSTOMER_WAIT_MINUTES + 10} phút", problem.detail)

    def test_an_answered_customer_is_not_waiting(self):
        self._message("INBOUND", CUSTOMER_WAIT_MINUTES + 10)
        self._message("OUTBOUND", CUSTOMER_WAIT_MINUTES + 9)
        self.assertIsNone(self.monitor.check_customers_waiting("acct-1"))

    def test_a_recent_message_is_not_yet_a_fault(self):
        self._message("INBOUND", CUSTOMER_WAIT_MINUTES - 5)
        self.assertIsNone(self.monitor.check_customers_waiting("acct-1"))

    def test_a_conversation_handed_to_a_person_is_not_this_checks_business(self):
        self._message("INBOUND", CUSTOMER_WAIT_MINUTES + 10)
        self.database.conn.execute(
            "INSERT INTO conversation_control(account_id, conversation_id, mode) "
            "VALUES ('acct-1', 'conv', 'HUMAN')"
        )
        self.database.conn.commit()
        self.assertIsNone(self.monitor.check_customers_waiting("acct-1"))

    def test_an_old_unanswered_thread_never_pages_forever(self):
        self._message("INBOUND", CUSTOMER_WAIT_LOOKBACK_HOURS * 60 + 30)
        self.assertIsNone(self.monitor.check_customers_waiting("acct-1"))

    def test_customers_waiting_is_scoped_to_one_account(self):
        other_shop = unscoped_connection(self, self.database)
        other_shop.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acct-2', 'tiktok_web')"
        )
        other_shop.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id) "
            "VALUES ('acct-2', 'conv')"
        )
        other_shop.execute(
            """INSERT INTO conversation_messages
                 (account_id, message_id, conversation_id, customer_id,
                  direction, text, created_at)
               VALUES ('acct-2', 'other', 'conv', 'cust', 'INBOUND', 'x',
                       now() - interval '1 hour')"""
        )
        self.database.conn.commit()
        self.assertIsNone(self.monitor.check_customers_waiting("acct-1"))

    # ------------------------------------------------------------------ SQL
    def test_unreadable_query_counts_only_stuck_unanswered_rows(self):
        self._ledger("kẹt-1", STUCK_ATTEMPTS)
        self._ledger("kẹt-2", STUCK_ATTEMPTS + 5)
        self._ledger("kẹt-3", STUCK_ATTEMPTS)
        # Must not count: too few attempts, and an already-settled thread.
        self._ledger("mới", 1)
        self._ledger("xong", STUCK_ATTEMPTS + 9, state="SETTLED")

        problem = self.monitor.check_unreadable_conversations("acct-1")

        self.assertIsNotNone(problem)
        self.assertIn("3 hội thoại", problem.title)

    def test_unreadable_query_is_scoped_to_one_account(self):
        """Two shops share these tables; one shop's trouble must never raise
        an alert on another's dashboard."""
        other_shop = unscoped_connection(self, self.database)
        other_shop.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acct-2', 'tiktok_web')"
        )
        for name in ("kẹt-1", "kẹt-2", "kẹt-3"):
            other_shop.execute(
                """INSERT INTO inbox_ledger
                     (account_id, conversation_key, conversation_name, state,
                      status, attempts, updated_at)
                   VALUES ('acct-2', %s, %s, 'UNANSWERED', 'no_inbound_message', %s, now())""",
                (name, name, STUCK_ATTEMPTS),
            )

        self.assertIsNone(self.monitor.check_unreadable_conversations("acct-1"))

    def test_task_failure_query_reads_both_counts(self):
        for i in range(9):
            self._task(f"fail-{i}", "failed")
        for i in range(3):
            self._task(f"ok-{i}", "completed")

        problem = self.monitor.check_task_failure_rate("acct-1")

        self.assertIsNotNone(problem)
        self.assertIn("9/12", problem.title)

    def test_task_failure_query_ignores_pending_and_old_rows(self):
        """Only finished work in the last hour can express a failure rate."""
        for i in range(20):
            self._task(f"pending-{i}", "pending")

        self.assertIsNone(self.monitor.check_task_failure_rate("acct-1"))

    def test_open_escalations_query_ignores_resolved_ones(self):
        for i in range(12):
            self._escalation(f"open-{i}")
        for i in range(5):
            self._escalation(f"done-{i}", status="RESOLVED")

        problem = self.monitor.check_open_escalations("acct-1")

        self.assertIsNotNone(problem)
        self.assertIn("12", problem.title)

    def test_a_healthy_account_raises_nothing(self):
        self.assertIsNone(self.monitor.check_unreadable_conversations("acct-1"))
        self.assertIsNone(self.monitor.check_task_failure_rate("acct-1"))
        self.assertIsNone(self.monitor.check_open_escalations("acct-1"))


if __name__ == "__main__":
    unittest.main()
