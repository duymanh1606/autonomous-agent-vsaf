"""The checks that turn a quietly degrading system into a notification.

These guard two things that are easy to get wrong in monitoring and painful
in production: a check that never fires (so trouble stays silent), and a
check that never clears (so an operator learns to ignore it).

Deliberately no database: every check is one SQL query whose *result* is what
the logic acts on, so the queries are stubbed and the decisions tested. The
SQL itself is exercised against real tables in
`test_health_monitor_queries.py`.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.health_monitor import (
    BACKUP_MAX_AGE_HOURS,
    HealthMonitor,
    STUCK_ATTEMPTS,
    STUCK_CONVERSATIONS,
    TASK_FAILURE_MIN_SAMPLE,
)


class StubNotifications:
    def __init__(self):
        self.opened = []
        self.resolved = []

    def record(self, *, account_id, kind, title, detail="", severity="info",
               dedup_key="", payload=None):
        self.opened.append({
            "account_id": account_id, "kind": kind, "dedup_key": dedup_key,
            "severity": severity, "title": title, "detail": detail,
        })

    def resolve_kind(self, account_id, kind, dedup_key):
        self.resolved.append((account_id, kind, dedup_key))


def monitor_with(rows, notifications=None, backup_dir=None):
    """A monitor whose every query returns `rows`."""
    monitor = HealthMonitor(
        notifications or StubNotifications(),
        database_url_resolver=lambda: "postgresql://unused",
        list_accounts=lambda: [SimpleNamespace(id="acct-1")],
        backup_dir=backup_dir or Path("/nonexistent-backups"),
    )
    monitor._query = lambda sql, params, **_: rows
    monitor.meminfo_path = PLENTY_OF_MEMORY
    return monitor


def _meminfo(total_mb, available_mb):
    import tempfile
    handle = tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False)
    handle.write(
        f"MemTotal:       {total_mb * 1024} kB\n"
        f"MemFree:        1024 kB\n"
        f"MemAvailable:   {available_mb * 1024} kB\n"
    )
    handle.close()
    return handle.name


# The real /proc/meminfo would make these tests depend on the machine.
PLENTY_OF_MEMORY = _meminfo(8000, 5000)


class UnreadableConversationTests(unittest.TestCase):
    """The early-warning signal for a TikTok layout change. The inbox watcher
    degrades quietly on purpose - it requeues rather than giving up - so
    without this the first symptom is a customer complaining."""

    def _stuck(self, count, attempts=STUCK_ATTEMPTS):
        return [(f"khach-{i}", attempts, "no_inbound_message") for i in range(count)]

    def test_several_stuck_conversations_raise_an_alert(self):
        monitor = monitor_with(self._stuck(STUCK_CONVERSATIONS))
        problem = monitor.check_unreadable_conversations("acct-1")
        self.assertIsNotNone(problem)
        self.assertEqual(problem.severity, "bad")
        self.assertIn("giao diện", problem.detail)

    def test_one_slow_render_is_not_an_alert(self):
        """A single thread that took a few extra scans to settle is ordinary;
        alerting on it would train the operator to ignore this check."""
        monitor = monitor_with(self._stuck(STUCK_CONVERSATIONS - 1))
        self.assertIsNone(monitor.check_unreadable_conversations("acct-1"))

    def test_nothing_stuck_is_not_an_alert(self):
        self.assertIsNone(monitor_with([]).check_unreadable_conversations("acct-1"))


class TaskFailureRateTests(unittest.TestCase):
    def test_a_high_failure_rate_on_a_real_sample_alerts(self):
        monitor = monitor_with([(30, 40)])
        problem = monitor.check_task_failure_rate("acct-1")
        self.assertIsNotNone(problem)
        self.assertIn("75%", problem.title + problem.detail)

    def test_a_small_sample_is_never_called_a_rate(self):
        """One failure out of two is noise. Calling that a 50% failure rate
        would fire this check on a quiet morning with almost no traffic."""
        monitor = monitor_with([(1, 2)])
        self.assertIsNone(monitor.check_task_failure_rate("acct-1"))

    def test_a_healthy_rate_on_a_real_sample_is_silent(self):
        monitor = monitor_with([(1, TASK_FAILURE_MIN_SAMPLE + 10)])
        self.assertIsNone(monitor.check_task_failure_rate("acct-1"))

    def test_no_tasks_at_all_is_silent(self):
        self.assertIsNone(monitor_with([(0, 0)]).check_task_failure_rate("acct-1"))


class EscalationBacklogTests(unittest.TestCase):
    def test_a_backlog_past_the_threshold_alerts(self):
        self.assertIsNotNone(monitor_with([(25,)]).check_open_escalations("acct-1"))

    def test_a_few_open_escalations_are_normal(self):
        self.assertIsNone(monitor_with([(2,)]).check_open_escalations("acct-1"))


class BackupFreshnessTests(unittest.TestCase):
    """A backup that quietly stopped running is worse than none, because it
    is trusted."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _archive(self, age_hours):
        import os
        from time import time
        path = self.dir / f"backup-{age_hours}.tar.gz"
        path.write_bytes(b"x")
        when = time() - age_hours * 3600
        os.utime(path, (when, when))
        return path

    def test_a_fresh_backup_is_silent(self):
        self._archive(2)
        monitor = monitor_with([], backup_dir=self.dir)
        self.assertIsNone(monitor.check_backup_freshness())

    def test_a_stale_backup_alerts(self):
        self._archive(BACKUP_MAX_AGE_HOURS + 12)
        monitor = monitor_with([], backup_dir=self.dir)
        problem = monitor.check_backup_freshness()
        self.assertIsNotNone(problem)
        self.assertEqual(problem.severity, "bad")

    def test_the_newest_archive_is_what_counts(self):
        """Old archives are kept by rotation; their age says nothing."""
        self._archive(BACKUP_MAX_AGE_HOURS + 100)
        self._archive(1)
        monitor = monitor_with([], backup_dir=self.dir)
        self.assertIsNone(monitor.check_backup_freshness())

    def test_no_backups_at_all_alerts(self):
        monitor = monitor_with([], backup_dir=self.dir)
        self.assertIsNotNone(monitor.check_backup_freshness())


class CustomersWaitingTests(unittest.TestCase):
    def test_waiting_customers_alert_with_the_longest_waits_named(self):
        problem = monitor_with([("Lan", 42), ("Minh", 20)]).check_customers_waiting("acct-1")
        self.assertEqual(problem.severity, "bad")
        self.assertIn("2 khách", problem.title)
        self.assertIn("Lan (42 phút)", problem.detail)

    def test_nobody_waiting_is_silent(self):
        self.assertIsNone(monitor_with([]).check_customers_waiting("acct-1"))

    def test_outside_operating_hours_is_never_a_fault(self):
        """The agent does not answer then, on purpose."""
        monitor = monitor_with([("Lan", 42)])
        monitor._is_open = lambda account_id: False
        self.assertIsNone(monitor.check_customers_waiting("acct-1"))

    def test_a_paused_or_stopped_workspace_is_never_a_fault(self):
        """An operator paused or stopped it: customers wait by their choice."""
        monitor = monitor_with([("Lan", 42)])
        monitor._is_answering = lambda account_id: False
        self.assertIsNone(monitor.check_customers_waiting("acct-1"))


class HeartbeatGateTests(unittest.TestCase):
    """A ping claims every check ran; a check that could not is a reason to
    stay quiet, so the outside monitor notices."""

    def test_a_clean_sweep_is_complete(self):
        monitor = monitor_with([])
        monitor.run_once()
        self.assertTrue(monitor.last_sweep_complete)

    def test_a_check_that_could_not_run_withholds_the_heartbeat(self):
        monitor = monitor_with([])

        def explode(account_id):
            raise RuntimeError("database down")

        monitor.check_task_failure_rate = explode
        monitor.run_once()
        self.assertFalse(monitor.last_sweep_complete)


class BackupOffsiteTests(unittest.TestCase):
    """deploy/backup.sh's own report of its last run (last-run.json)."""

    def monitor_with_status(self, status):
        import json
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if status is not None:
            (Path(tmp.name) / "last-run.json").write_text(json.dumps(status))
        return monitor_with([], backup_dir=Path(tmp.name))

    def test_a_failed_offsite_copy_alerts_even_with_a_fresh_local_archive(self):
        problem = self.monitor_with_status(
            {"ok": False, "remote": "failed", "error": "copy to BACKUP_REMOTE failed"},
        ).check_backup_offsite()
        self.assertEqual(problem.severity, "bad")
        self.assertIn("ra ngoài máy", problem.title)

    def test_a_refused_plaintext_copy_alerts(self):
        problem = self.monitor_with_status(
            {"ok": False, "remote": "refused_plaintext", "error": "refusing"},
        ).check_backup_offsite()
        self.assertEqual(problem.severity, "bad")

    def test_a_failed_run_alerts(self):
        problem = self.monitor_with_status(
            {"ok": False, "remote": "disabled", "error": "pg_dump failed"},
        ).check_backup_offsite()
        self.assertIn("thất bại", problem.title)
        self.assertIn("pg_dump failed", problem.detail)

    def test_local_only_is_a_dashboard_warning_not_a_page(self):
        problem = self.monitor_with_status(
            {"ok": True, "remote": "disabled", "error": ""},
        ).check_backup_offsite()
        self.assertEqual(problem.severity, "warn")

    def test_a_good_offsite_run_is_silent(self):
        self.assertIsNone(self.monitor_with_status(
            {"ok": True, "remote": "ok", "encrypted": True, "error": ""},
        ).check_backup_offsite())

    def test_no_run_yet_is_left_to_the_freshness_check(self):
        self.assertIsNone(self.monitor_with_status(None).check_backup_offsite())

    def test_an_encrypted_archive_counts_as_fresh(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "backup-now.tar.gz.age").write_bytes(b"x")
        self.assertIsNone(monitor_with([], backup_dir=Path(tmp.name)).check_backup_freshness())


class MemoryTests(unittest.TestCase):
    def check(self, total_mb, available_mb):
        monitor = monitor_with([])
        monitor.meminfo_path = _meminfo(total_mb, available_mb)
        return monitor.check_memory()

    def test_plenty_of_memory_is_silent(self):
        self.assertIsNone(self.check(8000, 4000))

    def test_little_absolute_memory_alerts(self):
        self.assertEqual(self.check(4000, 500).severity, "bad")

    def test_a_small_share_of_a_large_machine_alerts(self):
        self.assertIsNotNone(self.check(64000, 3000))


class PublishingTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from time import time
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backups = Path(self.tmp.name)
        # A healthy installation, so the backup check is silent and these
        # tests are about publishing rather than about backups.
        fresh = self.backups / "backup-now.tar.gz"
        fresh.write_bytes(b"x")
        os.utime(fresh, (time(), time()))

    def test_every_passing_check_clears_its_previous_alert(self):
        """Resolving matters as much as opening: an alert that never clears
        is one an operator stops reading."""
        notifications = StubNotifications()
        monitor = monitor_with([], notifications=notifications, backup_dir=self.backups)
        monitor.run_once()

        self.assertEqual(notifications.opened, [])
        resolved = {key for _, _, key in notifications.resolved}
        self.assertEqual(
            resolved,
            {"scan_unreadable", "escalations_backlog", "task_failures",
             "customers_waiting", "backup", "backup_offsite", "memory"},
        )

    def test_a_failing_check_opens_one_notification_under_a_stable_key(self):
        """The dedup key is what keeps a fault that persists for hours from
        becoming hours of identical notifications."""
        notifications = StubNotifications()
        monitor = monitor_with([(99,)], notifications=notifications, backup_dir=self.backups)
        monitor.run_once()
        monitor.run_once()

        backlog = [item for item in notifications.opened
                   if item["dedup_key"] == "escalations_backlog"]
        self.assertEqual(len(backlog), 2)
        self.assertTrue(all(item["kind"] == "health_check" for item in backlog))

    def test_one_broken_check_never_stops_the_others(self):
        """The monitor exists to report trouble; it must never become it."""
        notifications = StubNotifications()
        monitor = monitor_with([], notifications=notifications, backup_dir=self.backups)

        def explode(account_id):
            raise RuntimeError("query blew up")

        monitor.check_unreadable_conversations = explode
        monitor.run_once()

        resolved = {key for _, _, key in notifications.resolved}
        self.assertIn("task_failures", resolved)
        self.assertIn("backup", resolved)
        self.assertNotIn("scan_unreadable", resolved)


if __name__ == "__main__":
    unittest.main()
