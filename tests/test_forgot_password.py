""""Quên mật khẩu": a temporary password mailed to the account's own address.

The rules that make it safe rather than a new way in:

* the temporary password never replaces the real one until it is used -
  otherwise asking for a reset on someone else's address would lock them out;
* it expires, and works once;
* the session it opens can do nothing but choose a new password;
* the answer is the same whether or not the address has an account.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth, mailer
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres

EMAIL = "nhanvien.shop@gmail.com"
REAL = "real password 123"
NEW = "brand new password"


@requires_postgres
class ForgotPasswordTests(unittest.TestCase):
    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.users = UserStore(self.schema.dsn)
        self.addCleanup(self.users.close)

        real_store = main.users_store
        self.sent_mail: list[tuple[str, str, str]] = []
        self.mail_error: Exception | None = None

        def send(to, subject, body):
            if self.mail_error is not None:
                raise self.mail_error
            self.sent_mail.append((to, subject, body))

        patches = [
            mock.patch.object(main, "users_store", self.users),
            mock.patch.object(real_store, "get_by_id", side_effect=self.users.get_by_id),
            mock.patch.object(
                real_store, "accessible_account_ids",
                side_effect=self.users.accessible_account_ids,
            ),
            mock.patch.object(main.mailer, "send", side_effect=send),
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
        for limiter in (
            auth.forgot_password_limiter, auth.temp_password_email_limiter,
            auth.login_limiter, auth.login_ip_limiter, auth.login_global_limiter,
            auth.change_password_limiter,
        ):
            limiter._hits.clear()

        self.user = self.users.create_user(EMAIL, auth.hash_password(REAL), is_admin=False)
        self.admin = self.users.create_user("admin.shop@gmail.com", "hash", is_admin=True)

    # ------------------------------------------------------------- helpers
    def _forgot(self, email=EMAIL):
        with TestClient(main.app) as client:
            return client.post("/api/auth/forgot-password", json={"email": email})

    def _temp_password(self) -> str:
        body = [body for to, _, body in self.sent_mail if to == EMAIL][-1]
        return re.search(r"là: (\S+)", body).group(1)

    def _login(self, client, password):
        return client.post("/api/auth/login", json={"email": EMAIL, "password": password})

    # --------------------------------------------------------------- tests
    def test_mails_a_temporary_password_to_the_account_address(self):
        self.assertEqual(self._forgot().json(), {"ok": True})
        self.assertEqual([to for to, _, _ in self.sent_mail], [EMAIL])
        self.assertGreaterEqual(len(self._temp_password()), 12)

    def test_the_real_password_keeps_working_until_the_temporary_one_is_used(self):
        """Asking for a reset must not be a way to lock someone out."""
        self._forgot()
        with TestClient(main.app) as client:
            self.assertEqual(self._login(client, REAL).status_code, 200)

    def test_the_temporary_password_opens_a_session_that_can_only_change_it(self):
        self._forgot()
        temp = self._temp_password()
        with TestClient(main.app) as client:
            login = self._login(client, temp)
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json()["must_change_password"])
            self.assertTrue(client.get("/api/auth/me").json()["must_change_password"])
            self.assertEqual(client.get("/api/accounts").status_code, 403)

            changed = client.post(
                "/api/auth/change-password",
                json={"current_password": temp, "new_password": NEW},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertFalse(client.get("/api/auth/me").json()["must_change_password"])
            self.assertNotEqual(client.get("/api/accounts").status_code, 403)

        with TestClient(main.app) as client:
            self.assertEqual(self._login(client, NEW).status_code, 200)
        with TestClient(main.app) as client:
            # Used once - and the old password went with it.
            self.assertEqual(self._login(client, temp).status_code, 401)
            self.assertEqual(self._login(client, REAL).status_code, 401)

    def test_an_expired_temporary_password_does_not_work(self):
        self._forgot()
        temp = self._temp_password()
        with self.users._lock:
            self.users.connect().execute(
                "UPDATE users SET temp_password_expires_at = now() - interval '1 second'",
            )
        with TestClient(main.app) as client:
            self.assertEqual(self._login(client, temp).status_code, 401)
            self.assertEqual(self._login(client, REAL).status_code, 200)

    def test_the_answer_never_says_whether_the_address_has_an_account(self):
        unknown = self._forgot("nobody.here@gmail.com")
        known = self._forgot()
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.json(), known.json())
        self.assertEqual([to for to, _, _ in self.sent_mail], [EMAIL])

    def test_an_account_awaiting_approval_is_sent_nothing(self):
        self.users.set_active(self.user["id"], False)
        self.assertEqual(self._forgot().status_code, 200)
        self.assertEqual(self.sent_mail, [])

    def test_one_inbox_cannot_be_flooded(self):
        for _ in range(5):
            self.assertEqual(self._forgot().status_code, 200)
        self.assertEqual(len(self.sent_mail), 3)

    def test_a_mail_failure_looks_the_same_to_the_caller(self):
        self.mail_error = mailer.MailDeliveryFailed("SMTPServerDisconnected")
        self.assertEqual(self._forgot().json(), {"ok": True})

    def test_the_admin_button_mails_a_temporary_password_too(self):
        with TestClient(main.app) as admin:
            admin.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(self.admin["id"]))
            response = admin.post(f"/api/users/{self.user['id']}/reset-password-link")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([to for to, _, _ in self.sent_mail], [EMAIL])


class MailerTests(unittest.TestCase):
    def test_without_smtp_production_refuses_instead_of_writing_a_file(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": "", "DASHBOARD_ENV": "production"}):
            with self.assertRaises(mailer.MailNotConfigured):
                mailer.send("a@b.vn", "s", "b")

    def test_without_smtp_a_dev_machine_writes_an_owner_only_outbox(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.dict("os.environ", {"SMTP_HOST": "", "DASHBOARD_ENV": ""}), \
                mock.patch.object(mailer, "OUTBOX_PATH", Path(directory.name) / "outbox.log"):
            mailer.send("a@b.vn", "Mã", "123456")
            self.assertIn("123456", mailer.OUTBOX_PATH.read_text(encoding="utf-8"))
            self.assertEqual(mailer.OUTBOX_PATH.stat().st_mode & 0o777, 0o600)

    def test_with_smtp_it_logs_in_and_sends(self):
        env = {"SMTP_HOST": "smtp.example.test", "SMTP_USERNAME": "shop@example.test",
               "SMTP_PASSWORD": "app-password", "SMTP_SECURITY": "starttls", "SMTP_PORT": ""}
        with mock.patch.dict("os.environ", env), mock.patch.object(mailer.smtplib, "SMTP") as smtp:
            mailer.send("a@b.vn", "Mã", "123456")
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=mailer.SEND_TIMEOUT_SECONDS)
        client = smtp.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("shop@example.test", "app-password")
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
