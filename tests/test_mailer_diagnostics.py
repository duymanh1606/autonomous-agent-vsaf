"""Why a forgot-password mail did not arrive, answerable from the server.

Reported: forgot-password "does nothing". The page always answers "ok" - on
purpose, so it cannot be used to learn which addresses have accounts - and
the only trace was an exception class name in the log, so an operator could
not tell a wrong App Password from a blocked port from a rate limit. The
send path itself works end to end (checked against a local SMTP server).
Covered here: what the log and the test command now say, and the Gmail App
Password pasted with the spaces Google displays it with.
"""

import io
import os
import smtplib
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import mailer

GMAIL = {
    "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls",
    "SMTP_USERNAME": "shop@gmail.com", "SMTP_PASSWORD": "abcd efgh ijkl mnop",
}


class FakeSMTP:
    """Records the login; `refuse` makes it answer like Gmail on a bad password."""

    logins = []
    refuse = False

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        FakeSMTP.logins.append((username, password))
        if FakeSMTP.refuse:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted.")

    def send_message(self, message):
        pass


class MailerTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.logins, FakeSMTP.refuse = [], False
        for patcher in (
            mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP),
            # The test command probes the real network; not from the tests.
            mock.patch.object(mailer, "_probe", return_value="mở - 220 smtp.gmail.com ESMTP"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_gmail_app_password_pasted_with_spaces_logs_in_without_them(self):
        with mock.patch.dict(os.environ, GMAIL):
            mailer.send("user@gmail.com", "s", "b")
        self.assertEqual(FakeSMTP.logins, [("shop@gmail.com", "abcdefghijklmnop")])

    def test_another_providers_password_is_left_as_it_is(self):
        with mock.patch.dict(os.environ, {**GMAIL, "SMTP_HOST": "mail.shop.vn"}):
            mailer.send("user@gmail.com", "s", "b")
        self.assertEqual(FakeSMTP.logins[0][1], "abcd efgh ijkl mnop")

    def test_a_refused_login_says_what_the_server_said(self):
        FakeSMTP.refuse = True
        with mock.patch.dict(os.environ, GMAIL):
            with self.assertLogs("dashboard_api.mailer", level="WARNING") as logs:
                with self.assertRaises(mailer.MailDeliveryFailed) as caught:
                    mailer.send("user@gmail.com", "s", "b")
        self.assertIn("535", str(caught.exception))
        self.assertIn("ở bước login", str(caught.exception))
        self.assertIn("Username and Password not accepted", logs.output[0])
        self.assertNotIn("abcd", logs.output[0])
        self.assertNotIn("user@gmail.com", logs.output[0])

    def test_the_test_command_reports_the_failure(self):
        FakeSMTP.refuse = True
        out = io.StringIO()
        with mock.patch.dict(os.environ, GMAIL), redirect_stdout(out):
            with self.assertLogs("dashboard_api.mailer", level="WARNING"):
                code = mailer._diagnose("me@gmail.com")
        self.assertEqual(code, 1)
        self.assertIn("SMTP_PASSWORD = đã đặt, 16 ký tự", out.getvalue())
        self.assertIn("gửi thất bại", out.getvalue())
        self.assertIn("smtp.gmail.com:587 (starttls) -> mở - 220", out.getvalue())
        self.assertIn("535", out.getvalue())
        self.assertNotIn("abcd", out.getvalue())

    def test_the_test_command_reports_success(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, GMAIL), redirect_stdout(out):
            self.assertEqual(mailer._diagnose("me@gmail.com"), 0)
        self.assertIn("đã gửi tới me@gmail.com", out.getvalue())


if __name__ == "__main__":
    unittest.main()
