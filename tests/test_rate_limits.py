"""Login throttling on all three axes.

The email-only limiter that used to stand alone is good at what it was built
for - one person's typos must not lock out a shared office - and useless
against the attack that matters on a public URL: one source trying a single
common password against thousands of addresses. Every attempt lands on a
different email, so no counter ever fills.

Two more axes fix that without giving up the original property: per-IP (which
only became meaningful once `client_ip()` stopped believing headers) and a
blunt global ceiling for a spray spread across many sources.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth


class LoginLimitTests(unittest.TestCase):
    def setUp(self):
        for limiter in (auth.login_limiter, auth.login_ip_limiter,
                        auth.login_global_limiter, auth.register_limiter):
            limiter._hits.clear()
            self.addCleanup(limiter._hits.clear)

        patches = [
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
            # No such user: every attempt below is a failed login.
            mock.patch.object(main.users_store, "get_by_email", return_value=None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _login(client, email, password="wrong password"):
        return client.post(
            "/api/auth/login", json={"email": email, "password": password},
        )

    def test_repeated_guesses_at_one_address_are_locked_out(self):
        with TestClient(main.app) as client:
            for _ in range(auth.login_limiter.max_attempts):
                self.assertEqual(self._login(client, "victim@x.test").status_code, 401)

            blocked = self._login(client, "victim@x.test")

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_spraying_across_addresses_from_one_source_is_stopped(self):
        """The hole the email-only limiter left wide open."""
        with TestClient(main.app) as client:
            statuses = [
                self._login(client, f"user{index}@x.test").status_code
                for index in range(auth.login_ip_limiter.max_attempts + 5)
            ]

        self.assertEqual(statuses[0], 401)
        self.assertIn(429, statuses)
        self.assertEqual(statuses[auth.login_ip_limiter.max_attempts], 429)

    def test_one_address_does_not_exhaust_the_whole_ip_budget(self):
        """The per-email lock trips first, which is what keeps a shared office
        working: one person's typos cost 5 of the IP's 20."""
        with TestClient(main.app) as client:
            for _ in range(auth.login_limiter.max_attempts + 3):
                self._login(client, "clumsy@x.test")

            other = self._login(client, "colleague@x.test")

        self.assertEqual(other.status_code, 401)

    def test_a_forged_forwarded_header_does_not_reset_the_bucket(self):
        with TestClient(main.app) as client:
            for index in range(auth.login_ip_limiter.max_attempts):
                self._login(client, f"user{index}@x.test")

            forged = client.post(
                "/api/auth/login",
                json={"email": "next@x.test", "password": "wrong password"},
                headers={"x-forwarded-for": "198.51.100.23"},
            )

        self.assertEqual(forged.status_code, 429)

    def test_a_correct_password_clears_the_email_counter_but_not_the_source(self):
        """A right password for one account says nothing about the source, so
        the IP and global budgets have to stand."""
        row = {
            "id": 1, "email": "real@x.test", "is_admin": False, "is_active": True,
            "password_hash": auth.hash_password("correct horse"),
        }
        with mock.patch.object(main.users_store, "get_by_email", return_value=row), \
                mock.patch.object(main.users_store, "accessible_account_ids",
                                  return_value=[]):
            with TestClient(main.app) as client:
                for _ in range(3):
                    self._login(client, "real@x.test")
                self.assertEqual(
                    self._login(client, "real@x.test", "correct horse").status_code, 200,
                )

                self.assertEqual(auth.login_limiter._hits.get("real@x.test"), None)
                # TestClient's peer is "testclient", which is not a trusted
                # proxy - so client_ip() returns it unchanged, which is the
                # branch that matters: one source, one bucket.
                self.assertEqual(
                    [len(hits) for hits in auth.login_ip_limiter._hits.values()], [3],
                )

    def test_the_global_ceiling_catches_a_distributed_spray(self):
        """Neither the email nor the IP counter fills when every attempt uses
        a fresh address from a fresh source."""
        auth.login_global_limiter._hits.clear()
        for _ in range(auth.login_global_limiter.max_attempts):
            auth.login_global_limiter.hit(auth.LOGIN_GLOBAL_KEY)

        with TestClient(main.app) as client:
            blocked = self._login(client, "brand-new@x.test")

        self.assertEqual(blocked.status_code, 429)


class LimiterBudgetTests(unittest.TestCase):
    def test_the_budgets_are_the_ones_the_comments_describe(self):
        """A silent change to any of these is a silent change to how much
        brute force this dashboard tolerates."""
        self.assertEqual(
            (auth.login_limiter.max_attempts, auth.login_limiter.window_seconds),
            (5, 300),
        )
        self.assertEqual(
            (auth.login_ip_limiter.max_attempts, auth.login_ip_limiter.window_seconds),
            (20, 300),
        )
        self.assertEqual(
            (auth.login_global_limiter.max_attempts,
             auth.login_global_limiter.window_seconds),
            (200, 300),
        )
        # Generous per-IP, because every SSH-tunnel operator shares the single
        # loopback bucket - see client_ip().
        self.assertGreater(
            auth.login_ip_limiter.max_attempts, auth.login_limiter.max_attempts,
        )


if __name__ == "__main__":
    unittest.main()
