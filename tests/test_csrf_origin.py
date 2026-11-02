"""Cross-site state-changing requests, and why the check is opt-in.

`SameSite=Lax` already stops a cross-site POST carrying the session cookie,
but it is a control that lives in the browser and it says nothing about the
WebSocket handshake. This is the origin-side half.

The opt-in shape matters as much as the check: enforcement turns on only once
`DASHBOARD_TRUSTED_ORIGINS` (or `DASHBOARD_PUBLIC_URL`) names something. Every
`TestClient` call in this suite sends no `Origin`, as does any curl an
operator runs over an SSH tunnel, so enforcing unconditionally would have
meant a suite-wide failure and a locked-out operator rather than a security
improvement. The last test here is the one that protects the other 800.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main


TRUSTED = "https://app.example.com"


class CsrfOriginTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
            # Login is the convenient public POST to aim at; it must never
            # reach the user store in these tests.
            mock.patch.object(main.users_store, "get_by_email", return_value=None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _enforced():
        return mock.patch.dict("os.environ", {"DASHBOARD_TRUSTED_ORIGINS": TRUSTED})

    def _login(self, client, origin=None):
        headers = {"origin": origin} if origin else {}
        return client.post(
            "/api/auth/login", json={"email": "a@x.test", "password": "whatever"},
            headers=headers,
        )

    def test_a_hostile_origin_is_refused(self):
        with self._enforced(), TestClient(main.app) as client:
            response = self._login(client, "https://evil.test")

        self.assertEqual(response.status_code, 403)
        self.assertIn("origin", response.json()["detail"])

    def test_the_trusted_origin_gets_through_to_the_route(self):
        with self._enforced(), TestClient(main.app) as client:
            response = self._login(client, TRUSTED)

        # 401 = the route ran and rejected the credentials, which is the point:
        # the CSRF layer let it past.
        self.assertEqual(response.status_code, 401)

    def test_a_missing_origin_is_refused_while_enforcing(self):
        """Browsers always send Origin on these methods, so its absence means
        a non-browser client - which belongs on the loopback listener."""
        with self._enforced(), TestClient(main.app) as client:
            response = self._login(client)

        self.assertEqual(response.status_code, 403)

    def test_a_referer_stands_in_for_a_missing_origin(self):
        with self._enforced(), TestClient(main.app) as client:
            response = client.post(
                "/api/auth/login", json={"email": "a@x.test", "password": "x"},
                headers={"referer": f"{TRUSTED}/customers?tab=all"},
            )

        self.assertEqual(response.status_code, 401)

    def test_reads_are_never_blocked_by_origin(self):
        """A GET with a hostile Origin is not a CSRF risk - the cookie may
        ride along, but the response is unreadable cross-origin, and blocking
        it would break ordinary navigation."""
        with self._enforced(), TestClient(main.app) as client:
            response = client.get("/api/system", headers={"origin": "https://evil.test"})

        # 401 (not authenticated), not 403 (origin) - it reached the route.
        self.assertEqual(response.status_code, 401)

    def test_the_public_url_alone_is_enough_to_turn_enforcement_on(self):
        with mock.patch.dict("os.environ", {"DASHBOARD_PUBLIC_URL": TRUSTED + "/"}):
            with TestClient(main.app) as client:
                self.assertEqual(self._login(client, "https://evil.test").status_code, 403)
                self.assertEqual(self._login(client, TRUSTED).status_code, 401)

    def test_nothing_changes_while_no_allowlist_is_configured(self):
        """The compatibility guarantee the rest of the suite relies on."""
        with mock.patch.dict(
            "os.environ", {"DASHBOARD_TRUSTED_ORIGINS": "", "DASHBOARD_PUBLIC_URL": ""},
        ):
            with TestClient(main.app) as client:
                self.assertEqual(self._login(client).status_code, 401)
                self.assertEqual(
                    self._login(client, "https://evil.test").status_code, 401,
                )


class OriginParsingTests(unittest.TestCase):
    def test_the_origin_header_wins_and_is_normalised(self):
        from dashboard_api import auth

        self.assertEqual(
            auth.request_origin({"origin": "https://app.example.com/"}),
            "https://app.example.com",
        )

    def test_a_null_origin_is_not_an_origin(self):
        """Sandboxed iframes and some redirects send the literal string."""
        from dashboard_api import auth

        self.assertEqual(auth.request_origin({"origin": "null"}), "")

    def test_a_referer_is_cut_back_to_its_origin(self):
        from dashboard_api import auth

        self.assertEqual(
            auth.request_origin({"referer": "https://app.example.com:8443/a/b?c=d"}),
            "https://app.example.com:8443",
        )


if __name__ == "__main__":
    unittest.main()
