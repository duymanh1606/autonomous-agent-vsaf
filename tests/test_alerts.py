"""Alerting that reaches people who are not looking at the dashboard.

Run against the real `notifications` table through the real NotificationStore:
the episode bookkeeping (when a fault opened, whether it was mailed, whether
its recovery was) lives in SQL, and a mistake there means either silence or a
mail on every sweep - both worse than no alerting at all.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import alerts
from dashboard_api.alerts import AlertDispatcher, EscalationMailer, ping_heartbeat
from dashboard_api.notifications import NotificationStore
from dashboard_api.users import UserStore
from tests.support import make_database


class Outbox:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, to, subject, body):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, subject, body))


class AlertDispatcherTests(unittest.TestCase):
    def setUp(self):
        database = make_database(self, account_id="acct-1")
        self.store = NotificationStore(database.dsn)
        self.addCleanup(self.store.close)
        self.outbox = Outbox()
        self.dispatcher = AlertDispatcher(
            self.store, send=self.outbox, label_for=lambda account_id: f"Shop {account_id}",
        )
        patcher = mock.patch.dict(os.environ, {"ALERT_EMAILS": "a@x.com, b@x.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fault(self, key="customers_waiting", severity="bad", account_id="acct-1"):
        self.store.record(
            account_id=account_id, kind="health_check", dedup_key=key,
            severity=severity, title=f"Lỗi {key}", detail="chi tiết",
        )

    def _age(self, minutes=10):
        """Pretend every open fault started `minutes` ago."""
        self.store.connect().execute(
            "UPDATE notifications SET opened_at = now() - make_interval(mins => %s::int)",
            (minutes,),
        )

    def test_a_lasting_fault_is_mailed_once_to_every_recipient(self):
        self._fault()
        self._age()

        self.assertEqual(self.dispatcher.run_once(), 1)
        self.assertEqual([to for to, _, _ in self.outbox.sent], ["a@x.com", "b@x.com"])
        subject, body = self.outbox.sent[0][1:]
        self.assertIn("[Cảnh báo] Shop acct-1: Lỗi customers_waiting", subject)
        self.assertIn("chi tiết", body)

        # Re-recorded on every monitor sweep while it lasts: still one mail.
        self._fault()
        self.assertEqual(self.dispatcher.run_once(), 0)
        self.assertEqual(len(self.outbox.sent), 2)

    def test_a_fault_inside_the_grace_period_is_not_mailed(self):
        """A routine agent restart drops the stream for seconds; that is not
        worth waking anyone."""
        self._fault()
        self.assertEqual(self.dispatcher.run_once(), 0)
        self.assertEqual(self.outbox.sent, [])

    def test_re_recording_an_open_fault_does_not_restart_its_clock(self):
        """The monitor re-records every 5 minutes; if that reset the start,
        a lasting fault would never get past the grace period."""
        self._fault()
        self._age()
        self._fault()
        self.assertEqual(self.dispatcher.run_once(), 1)

    def test_warnings_stay_on_the_dashboard(self):
        self._fault(severity="warn")
        self._age()
        self.assertEqual(self.dispatcher.run_once(), 0)

    def test_recovery_is_mailed_once_and_only_for_mailed_faults(self):
        self._fault("mailed")
        self._age()
        self.dispatcher.run_once()
        # Resolved before it was ever mailed: nobody needs to hear it cleared.
        self._fault("brief")
        self.store.resolve_kind("acct-1", "health_check", "brief")
        self.store.resolve_kind("acct-1", "health_check", "mailed")

        self.assertEqual(self.dispatcher.run_once(), 1)
        subject, body = self.outbox.sent[-1][1:]
        self.assertIn("[Đã khắc phục] Shop acct-1: Lỗi mailed", subject)
        self.assertNotIn("brief", body)
        self.assertEqual(self.dispatcher.run_once(), 0)

    def test_a_fault_that_comes_back_is_a_new_episode(self):
        self._fault()
        self._age()
        self.dispatcher.run_once()
        self.store.resolve_kind("acct-1", "health_check", "customers_waiting")
        self.dispatcher.run_once()

        self._fault()
        self.assertEqual(self.dispatcher.run_once(), 0)  # new grace period
        self._age()
        self.assertEqual(self.dispatcher.run_once(), 1)
        self.assertIn("[Cảnh báo]", self.outbox.sent[-1][1])

    def test_many_faults_go_out_as_one_mail(self):
        """A TikTok layout change hits every workspace at once."""
        for account_id in ("acct-1", "acct-2", "acct-3"):
            self._fault(account_id=account_id)
        self._age()
        self.assertEqual(self.dispatcher.run_once(), 3)
        self.assertEqual(len(self.outbox.sent), 2)  # one per recipient
        self.assertIn("(+2 sự cố khác)", self.outbox.sent[0][1])

    def test_a_held_back_fault_is_not_marked(self):
        """Held back now (a stopped workspace), it must still go out if it
        later becomes real."""
        self._fault()
        self._age()
        self.dispatcher.should_alert = lambda row: False
        self.assertEqual(self.dispatcher.run_once(), 0)
        self.dispatcher.should_alert = lambda row: True
        self.assertEqual(self.dispatcher.run_once(), 1)

    def test_a_failed_send_is_retried_on_the_next_sweep(self):
        self._fault()
        self._age()
        self.dispatcher._send = Outbox(fail=True)
        self.assertEqual(self.dispatcher.run_once(), 0)
        self.dispatcher._send = self.outbox
        self.assertEqual(self.dispatcher.run_once(), 1)

    def test_no_recipients_means_dashboard_only(self):
        self._fault()
        self._age()
        with mock.patch.dict(os.environ, {"ALERT_EMAILS": ""}):
            self.assertEqual(self.dispatcher.run_once(), 0)
        self.assertEqual(self.outbox.sent, [])


class EscalationMailerTests(unittest.TestCase):
    """A conversation handed to a person, mailed to that workspace's people."""

    def setUp(self):
        database = make_database(self, account_id="acct-1")
        self.store = NotificationStore(database.dsn)
        self.addCleanup(self.store.close)
        self.users = UserStore(database.dsn)
        self.addCleanup(self.users.close)
        self.outbox = Outbox()
        self.mailer = EscalationMailer(
            self.store, recipients_for=self.users.workspace_emails,
            send=self.outbox, label_for=lambda account_id: f"Shop {account_id}",
        )
        patcher = mock.patch.dict(os.environ, {"ESCALATION_EMAILS": "", "SMTP_HOST": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _user(self, email, *account_ids, active=True, admin=False):
        user = self.users.create_user(email, "hash", is_admin=admin, is_active=active)
        for account_id in account_ids:
            self.users.grant_access(user["id"], account_id)
        return user

    def _escalate(self, conversation="Lan", account_id="acct-1",
                  reason="routed_to_human:complaint", text="hàng bị lỗi"):
        self.store.from_event({
            "account_id": account_id, "type": "escalation.opened",
            "payload": {"conversation": conversation, "reason": reason, "text": text},
        })

    def _age(self, seconds=120):
        self.store.connect().execute(
            "UPDATE notifications SET opened_at = now() - make_interval(secs => %s)",
            (seconds,),
        )

    def test_mailed_once_to_everyone_granted_the_workspace(self):
        self._user("owner@gmail.com", "acct-1")
        self._user("staff@gmail.com", "acct-1")
        self._escalate()
        self._age()

        self.assertEqual(self.mailer.run_once(), 1)
        self.assertEqual(
            sorted(to for to, _, _ in self.outbox.sent),
            ["owner@gmail.com", "staff@gmail.com"],
        )
        subject, body = self.outbox.sent[0][1:]
        self.assertIn("[Chờ tiếp quản] Shop acct-1: Cần người xử lý: Lan", subject)
        self.assertIn("routed_to_human:complaint", body)

        # The agent escalates the same thread again on the next scan.
        self._escalate()
        self.assertEqual(self.mailer.run_once(), 0)
        self.assertEqual(len(self.outbox.sent), 2)

    def test_the_customers_words_are_not_mailed(self):
        self._user("owner@gmail.com", "acct-1")
        self._escalate(text="số nhà 12 ngõ 3")
        self._age()
        self.mailer.run_once()
        self.assertNotIn("số nhà 12", self.outbox.sent[0][2])

    def test_a_shop_never_appears_in_another_shops_mail(self):
        self._user("a@gmail.com", "acct-1")
        self._user("b@gmail.com", "acct-2")
        self._user("admin@gmail.com", admin=True)  # not granted: not mailed
        self._escalate("Lan", account_id="acct-1")
        self._escalate("Minh", account_id="acct-2")
        self._age()

        self.assertEqual(self.mailer.run_once(), 2)
        sent = {to: body for to, _, body in self.outbox.sent}
        self.assertEqual(sorted(sent), ["a@gmail.com", "b@gmail.com"])
        self.assertIn("Lan", sent["a@gmail.com"])
        self.assertNotIn("Minh", sent["a@gmail.com"])
        self.assertIn("Minh", sent["b@gmail.com"])
        self.assertNotIn("Lan", sent["b@gmail.com"])

    def test_inactive_users_are_not_mailed(self):
        self._user("gone@gmail.com", "acct-1", active=False)
        self._escalate()
        self._age()
        self.assertEqual(self.mailer.run_once(), 0)
        self.assertEqual(self.outbox.sent, [])
        # Nobody to tell: not looked up again on every run.
        self.assertEqual(self.store.pending_escalations(grace_seconds=0), [])

    def test_one_handled_inside_the_grace_period_is_not_mailed(self):
        self._user("owner@gmail.com", "acct-1")
        self._escalate()
        self.assertEqual(self.mailer.run_once(), 0)
        notification = self.store.list(account_id="acct-1")[0]
        self.store.resolve(notification["id"])
        self._age()
        self.assertEqual(self.mailer.run_once(), 0)
        self.assertEqual(self.outbox.sent, [])

    def test_a_failed_send_is_retried(self):
        self._user("owner@gmail.com", "acct-1")
        self._escalate()
        self._age()
        self.mailer._send = Outbox(fail=True)
        self.assertEqual(self.mailer.run_once(), 0)
        self.mailer._send = self.outbox
        self.assertEqual(self.mailer.run_once(), 1)

    def test_switched_off(self):
        self._user("owner@gmail.com", "acct-1")
        self._escalate()
        self._age()
        with mock.patch.dict(os.environ, {"ESCALATION_EMAILS": "off"}):
            self.assertEqual(self.mailer.run_once(), 0)
        self.assertEqual(self.outbox.sent, [])

    def test_no_recovery_mail_when_it_is_handled(self):
        """The ALERT_EMAILS path must not report a handled conversation as
        a fault that cleared."""
        self._user("owner@gmail.com", "acct-1")
        self._escalate()
        self._age()
        self.mailer.run_once()
        self.store.resolve(self.store.list(account_id="acct-1")[0]["id"])

        dispatcher = AlertDispatcher(self.store, send=self.outbox)
        with mock.patch.dict(os.environ, {"ALERT_EMAILS": "ops@x.com"}):
            self.assertEqual(dispatcher.run_once(), 0)


class HeartbeatTests(unittest.TestCase):
    def test_unset_url_pings_nothing(self):
        with mock.patch.dict(os.environ, {"ALERT_HEARTBEAT_URL": ""}), \
                mock.patch.object(alerts.httpx, "get") as get:
            self.assertFalse(ping_heartbeat())
        get.assert_not_called()

    def test_ping_goes_to_the_configured_url(self):
        url = "https://hc-ping.com/abc"
        response = httpx.Response(200, request=httpx.Request("GET", url))
        with mock.patch.dict(os.environ, {"ALERT_HEARTBEAT_URL": url}), \
                mock.patch.object(alerts.httpx, "get", return_value=response) as get:
            self.assertTrue(ping_heartbeat())
        self.assertEqual(get.call_args.args[0], url)

    def test_a_failed_ping_never_raises(self):
        with mock.patch.dict(os.environ, {"ALERT_HEARTBEAT_URL": "https://hc-ping.com/abc"}), \
                mock.patch.object(alerts.httpx, "get", side_effect=httpx.ConnectError("down")):
            self.assertFalse(ping_heartbeat())


if __name__ == "__main__":
    unittest.main()
