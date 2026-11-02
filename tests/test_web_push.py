"""Web Push: who gets a push, for what, and what happens to dead subscriptions.

Run against the real tables through the real PushStore: who receives a push
is a join over users and workspace grants, and a mistake there is one shop's
customers on another shop's screen.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pywebpush import WebPushException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.users import UserStore
from dashboard_api.web_push import PushStore, WebPushNotifier, describe, endpoint_allowed
from tests.support import make_database


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class PushService:
    """Stands in for pywebpush.webpush; `gone` endpoints answer HTTP 410."""

    def __init__(self, gone=()):
        self.sent = []
        self.gone = set(gone)

    def __call__(self, *, subscription_info, data, **kwargs):
        endpoint = subscription_info["endpoint"]
        if endpoint in self.gone:
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
        self.sent.append((endpoint, data, kwargs))


def _ts(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _escalation(account_id="acct-1", conversation="Lan", seconds_ago=0, **payload):
    return {
        "account_id": account_id, "type": "escalation.opened", "ts": _ts(seconds_ago),
        "payload": {
            "conversation": conversation, "conversation_id": f"id-{conversation}",
            "reason": "routed_to_human:complaint", "text": "hàng bị lỗi rồi", **payload,
        },
    }


class EndpointTests(unittest.TestCase):
    def test_browser_push_services_are_accepted(self):
        for endpoint in (
            "https://fcm.googleapis.com/fcm/send/abc",
            "https://updates.push.services.mozilla.com/wpush/v2/abc",
            "https://web.push.apple.com/QGx",
            "https://wns2-par02p.notify.windows.com/w/?token=abc",
        ):
            self.assertTrue(endpoint_allowed(endpoint), endpoint)

    def test_anything_else_is_refused(self):
        """The supervisor POSTs to this URL: it must not be a way to make the
        server call an internal or arbitrary address."""
        for endpoint in (
            "http://fcm.googleapis.com/fcm/send/abc",
            "https://127.0.0.1:28900/control/pause",
            "https://fcm.googleapis.com.evil.test/x",
            "https://evilfcm.googleapis.com.test/x",
            "https://example.com/push.apple.com",
            "not a url",
            "https://fcm.googleapis.com/" + "a" * 2000,
        ):
            self.assertFalse(endpoint_allowed(endpoint), endpoint)


class DescribeTests(unittest.TestCase):
    def test_an_escalation_names_the_customer_but_not_their_words(self):
        content = describe(_escalation(), "Shop A")
        self.assertEqual(content["title"], "Chờ tiếp quản - Shop A")
        self.assertIn("Lan", content["body"])
        self.assertNotIn("hàng bị lỗi", content["body"])
        self.assertEqual(content["conversation_id"], "id-Lan")
        # Same tag as the in-page notification, so the two replace each other.
        self.assertEqual(content["tag"], "acct-1:escalation.opened:id-Lan")

    def test_a_known_reason_reads_as_a_sentence(self):
        content = describe(_escalation(reason="reply_withheld:order_ready"), "Shop A")
        self.assertIn("Đơn hàng đủ thông tin", content["body"])

    def test_routine_events_are_not_pushed(self):
        for event_type in ("agent.status", "health.changed", "task.completed"):
            self.assertIsNone(describe({"type": event_type, "account_id": "a"}, "Shop"))


class PushDeliveryTests(unittest.TestCase):
    def setUp(self):
        database = make_database(self, account_id="acct-1")
        self.users = UserStore(database.dsn)
        self.addCleanup(self.users.close)
        self.users.connect()  # the subscriptions table references users
        self.store = PushStore(database.dsn)
        self.addCleanup(self.store.close)
        self.dsn = database.dsn
        self.service = PushService()
        self.notifier = WebPushNotifier(
            self.store, send=self.service, executor=InlineExecutor(),
            label_for=lambda account_id: f"Shop {account_id}",
        )

    def _browser(self, email, *account_ids, active=True, endpoint=None):
        user = self.users.create_user(email, "hash", is_admin=False, is_active=active)
        for account_id in account_ids:
            self.users.grant_access(user["id"], account_id)
        endpoint = endpoint or f"https://fcm.googleapis.com/fcm/send/{email}"
        self.store.save(user_id=user["id"], endpoint=endpoint, p256dh="key", auth="secret")
        return user, endpoint

    def _endpoints(self):
        return sorted(endpoint for endpoint, _, _ in self.service.sent)

    def test_the_vapid_key_is_created_once_and_survives_a_restart(self):
        first = self.store.public_key()
        self.assertEqual(len(first), 87)  # 65-byte P-256 point, base64url
        again = PushStore(self.dsn)
        self.addCleanup(again.close)
        self.assertEqual(again.public_key(), first)

    def test_pushed_to_every_browser_of_everyone_granted_the_workspace(self):
        _, owner = self._browser("owner@gmail.com", "acct-1")
        _, staff = self._browser("staff@gmail.com", "acct-1")
        _, other_shop = self._browser("b@gmail.com", "acct-2")
        _, inactive = self._browser("gone@gmail.com", "acct-1", active=False)
        admin = self.users.create_user("admin@gmail.com", "hash", is_admin=True)
        self.store.save(
            user_id=admin["id"], endpoint="https://fcm.googleapis.com/fcm/send/admin",
            p256dh="key", auth="secret",
        )

        self.assertTrue(self.notifier.on_event(_escalation()))
        self.assertEqual(self._endpoints(), sorted([owner, staff]))
        self.assertNotIn(other_shop, self._endpoints())
        self.assertNotIn(inactive, self._endpoints())
        data = self.service.sent[0][1]
        self.assertIn("Shop acct-1", data)
        self.assertNotIn("hàng bị lỗi", data)

    def test_a_replayed_old_event_is_not_pushed(self):
        """An agent replays its latest event to every new stream, so a
        supervisor restart re-forwards an escalation handled long ago."""
        self._browser("owner@gmail.com", "acct-1")
        self.assertFalse(self.notifier.on_event(_escalation(seconds_ago=600)))
        self.assertFalse(self.notifier.on_event({**_escalation(), "ts": "garbage"}))
        self.assertEqual(self.service.sent, [])

    def test_the_same_conversation_is_pushed_once_in_a_while(self):
        self._browser("owner@gmail.com", "acct-1")
        self.assertTrue(self.notifier.on_event(_escalation()))
        self.assertFalse(self.notifier.on_event(_escalation()))
        self.assertTrue(self.notifier.on_event(_escalation(conversation="Minh")))
        self.assertEqual(len(self.service.sent), 2)

    def test_a_subscription_the_browser_dropped_is_removed(self):
        user, endpoint = self._browser("owner@gmail.com", "acct-1")
        self.service.gone.add(endpoint)
        self.notifier.on_event(_escalation())
        self.assertEqual(self.store.for_user(user["id"]), [])

    def test_a_browser_moves_to_whoever_subscribed_it_last(self):
        first, endpoint = self._browser("a@gmail.com", "acct-1")
        second, _ = self._browser("b@gmail.com", endpoint=endpoint)
        self.assertEqual(self.store.for_user(first["id"]), [])
        self.assertEqual(len(self.store.for_user(second["id"])), 1)

    def test_unsubscribing_only_touches_the_callers_own_browser(self):
        first, endpoint = self._browser("a@gmail.com", "acct-1")
        second, _ = self._browser("b@gmail.com", "acct-1")
        self.assertFalse(self.store.delete(endpoint, user_id=second["id"]))
        self.assertTrue(self.store.delete(endpoint, user_id=first["id"]))

    def test_send_test_reaches_only_the_callers_browsers(self):
        user, endpoint = self._browser("a@gmail.com")
        self._browser("b@gmail.com")
        self.assertEqual(self.notifier.send_test(user["id"]), 1)
        self.assertEqual(self._endpoints(), [endpoint])

    def test_switched_off(self):
        import os
        from unittest import mock

        self._browser("owner@gmail.com", "acct-1")
        with mock.patch.dict(os.environ, {"WEB_PUSH": "off"}):
            self.assertFalse(self.notifier.on_event(_escalation()))
        self.assertEqual(self.service.sent, [])


if __name__ == "__main__":
    unittest.main()
