"""A signed-in user changing their own password (POST /api/auth/change-password).

Three things matter beyond "the hash changes":

* the current password is required even with a valid session - someone at an
  unlocked machine must not be able to lock the owner out;
* a wrong current password is 400, never 401, because the dashboard treats any
  401 as "session gone" and signs the user out;
* every *other* session for the account dies, while the browser that made the
  change keeps working.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres


OLD = "old password 123"
NEW = "new password 456"


@requires_postgres
class ChangePasswordTests(unittest.TestCase):
    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.users = UserStore(self.schema.dsn)
        self.addCleanup(self.users.close)

        real_store = main.users_store
        patches = [
            mock.patch.object(main, "users_store", self.users),
            mock.patch.object(real_store, "get_by_id", side_effect=self.users.get_by_id),
            mock.patch.object(
                real_store, "accessible_account_ids",
                side_effect=self.users.accessible_account_ids,
            ),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        auth.change_password_limiter._hits.clear()
        auth.login_limiter._hits.clear()
        auth.login_ip_limiter._hits.clear()
        auth.login_global_limiter._hits.clear()

        self.user = self.users.create_user("nhanvien01@shop.vn", auth.hash_password(OLD), is_admin=False)

    def _client(self) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(self.user["id"]))
        return client

    def _change(self, client, current=OLD, new=NEW):
        return client.post(
            "/api/auth/change-password",
            json={"current_password": current, "new_password": new},
        )

    def _can_log_in_with(self, password: str) -> bool:
        with TestClient(main.app) as client:
            return client.post(
                "/api/auth/login", json={"email": "nhanvien01@shop.vn", "password": password},
            ).status_code == 200

    def test_the_new_password_replaces_the_old_one(self):
        with self._client() as client:
            self.assertEqual(self._change(client).status_code, 200)
        self.assertTrue(self._can_log_in_with(NEW))
        self.assertFalse(self._can_log_in_with(OLD))

    def test_a_wrong_current_password_is_refused_without_signing_out(self):
        with self._client() as client:
            response = self._change(client, current="not my password")
            self.assertEqual(response.status_code, 400)
            self.assertIn("mật khẩu hiện tại", response.json()["detail"])
            self.assertEqual(client.get("/api/auth/me").status_code, 200)
        self.assertTrue(self._can_log_in_with(OLD))

    def test_new_password_rules(self):
        with self._client() as client:
            self.assertEqual(self._change(client, new="short").status_code, 400)
            self.assertEqual(self._change(client, new=OLD).status_code, 400)
        self.assertTrue(self._can_log_in_with(OLD))

    def test_needs_a_session(self):
        with TestClient(main.app) as client:
            self.assertEqual(self._change(client).status_code, 401)

    def test_other_sessions_are_signed_out_but_this_one_keeps_working(self):
        with self._client() as other_device, self._client() as this_device:
            # iat has sub-second resolution; make sure the other device's token
            # is strictly older than the watermark the change will set.
            time.sleep(0.01)
            self.assertEqual(self._change(this_device).status_code, 200)
            self.assertEqual(this_device.get("/api/auth/me").status_code, 200)
            self.assertEqual(other_device.get("/api/auth/me").status_code, 401)

    def test_guessing_the_current_password_is_rate_limited(self):
        with self._client() as client:
            for _ in range(5):
                self.assertEqual(self._change(client, current="guess").status_code, 400)
            # Locked even for the right password until the window passes.
            self.assertEqual(self._change(client).status_code, 429)
        self.assertTrue(self._can_log_in_with(OLD))


if __name__ == "__main__":
    unittest.main()
