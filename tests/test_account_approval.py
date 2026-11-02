"""Self-registration, and the gap between signing up and being able to do
anything.

Registration stays open, but it no longer produces a usable account. Before,
`POST /api/auth/register` created a user, set a session cookie on the
response, and handed the caller a logged-in session on their first ever
request - on a public URL that means a stranger could immediately create
workspaces, each of which spawns a Chrome and an agent process on this host.

The second half is about a taken address. This is an internal dashboard for
one shop's staff, so a duplicate registration is reported plainly (409) -
someone retyping an address they already signed up with has to be told, or
they wait forever for an approval that is never coming. That is an operator
decision with a known cost: it makes enumeration possible.

What keeps it bounded is the rate limit, and that is why the budget is spent
**up front** rather than only on the success path. Charging only successes
would make every "already registered" answer free, turning a bounded guess
into an unlimited one. The test below pins that, and it matters more now
than when the responses were identical.

`forgot-password` keeps the opposite discipline and stays silent on purpose -
see test_rate_limits.py. The asymmetry is intentional: there, the answer also
tells an attacker which addresses are worth attacking.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres


@requires_postgres
class RegistrationApprovalTests(unittest.TestCase):
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

        # Each test gets a fresh budget; the limiter is process-global.
        auth.register_limiter._hits.clear()
        auth.login_limiter._hits.clear()
        auth.login_ip_limiter._hits.clear()
        auth.login_global_limiter._hits.clear()

        self.admin = self.users.create_user("admin@x.test", "hash", is_admin=True)

    def _client(self, user_row=None) -> TestClient:
        client = TestClient(main.app)
        if user_row is not None:
            client.cookies.set(
                auth.SESSION_COOKIE, auth.create_session_token(user_row["id"]),
            )
        return client

    def _register(self, client, email="newuser@gmail.com", password="a good password"):
        return client.post(
            "/api/auth/register", json={"email": email, "password": password},
        )

    # --------------------------------------------------------- registration
    def test_a_signup_is_created_inactive(self):
        with self._client() as client:
            response = self._register(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "pending_approval": True})

        row = self.users.get_by_email("newuser@gmail.com")
        self.assertFalse(row["is_active"])
        self.assertFalse(row["is_admin"])
        # And no workspace of their own: only an admin creates those, and an
        # admin grants access to one afterwards.
        self.assertEqual(self.users.accessible_account_ids(row["id"]), [])

    def test_a_signup_is_not_logged_in(self):
        """The cookie this used to set was a logged-in session for someone
        nobody had approved."""
        with self._client() as client:
            response = self._register(client)

            self.assertNotIn("set-cookie", {k.lower() for k in response.headers})
            self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_an_unapproved_user_cannot_log_in(self):
        with self._client() as client:
            self._register(client, password="a good password")
            response = client.post(
                "/api/auth/login",
                json={"email": "newuser@gmail.com", "password": "a good password"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("phê duyệt", response.json()["detail"])

    def test_an_unapproved_user_reaches_no_api_at_all(self):
        """Belt and braces: even handed a valid cookie, the middleware refuses
        an inactive row on every request."""
        with self._client() as client:
            self._register(client)
        row = self.users.get_by_email("newuser@gmail.com")

        with self._client(row) as client:
            for path in ("/api/auth/me", "/api/accounts", "/api/system",
                         "/api/notifications"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 401)

    # ------------------------------------------------- a duplicate address
    def test_a_taken_address_is_reported_plainly(self):
        """409 with a message the person can act on, rather than a second
        "waiting for an admin" for an approval that will never come."""
        with self._client() as client:
            first = self._register(client, email="takenuser@gmail.com")
            second = self._register(client, email="takenuser@gmail.com")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertIn("đã được đăng ký", second.json()["detail"])
        # And the refused attempt neither overwrote nor duplicated the row -
        # otherwise re-registering would be a way to reset someone's password.
        rows = [u for u in self.users.list_users() if u["email"] == "takenuser@gmail.com"]
        self.assertEqual(len(rows), 1)

    def test_a_refused_duplicate_does_not_reset_the_existing_password(self):
        """The attack this shuts down: register over an address you do not
        own, with a password you choose, and wait for an admin to approve."""
        with self._client() as client:
            self._register(client, email="takenuser@gmail.com", password="the real password")
            self._register(client, email="takenuser@gmail.com", password="an attacker's one")

        row = self.users.get_by_email("takenuser@gmail.com")
        self.assertTrue(auth.verify_password("the real password", row["password_hash"]))
        self.assertFalse(auth.verify_password("an attacker's one", row["password_hash"]))

    def test_a_taken_address_still_costs_budget(self):
        """What bounds the enumeration the 409 above makes possible. Five
        attempts is the whole hourly budget, whether or not any of them
        create anything - so a refused duplicate is never free."""
        with self._client() as client:
            self._register(client, email="takenuser@gmail.com")
            for _ in range(4):
                self._register(client, email="takenuser@gmail.com")

            exhausted = self._register(client, email="anotheruser@gmail.com")

        self.assertEqual(exhausted.status_code, 429)

    def test_a_forged_forwarded_header_does_not_buy_a_fresh_budget(self):
        """The end-to-end version of tests/test_client_ip_trust.py: a new
        X-Forwarded-For per request used to mean an unlimited number of
        registration buckets."""
        with self._client() as client:
            for index in range(5):
                self._register(
                    client, email=f"user{index}abc@gmail.com",
                    password="a good password",
                )
            blocked = client.post(
                "/api/auth/register",
                json={"email": "sneakyuser@gmail.com", "password": "a good password"},
                headers={"x-forwarded-for": "203.0.113.99"},
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertIsNone(self.users.get_by_email("sneakyuser@gmail.com"))

    def test_a_malformed_signup_is_still_reported_precisely(self):
        """Shape errors say nothing about who has an account, so they stay
        specific - a user mistyping their address deserves to know."""
        with self._client() as client:
            self.assertEqual(self._register(client, password="short").status_code, 400)
        self.assertIsNone(self.users.get_by_email("newuser@gmail.com"))

    # ------------------------------------------------------------ gmail only
    def test_a_new_account_must_be_a_gmail_address(self):
        refused = (
            "nhanvien01",             # not an address at all
            "nhanvien@shop.vn",       # not Gmail
            "abc@gmail.com",          # under 6 characters
            "nhan..vien@gmail.com",   # two dots in a row
            ".nhanvien@gmail.com",    # starts with a dot
            "nhanvien.@gmail.com",    # ends with a dot
            "nhan_vien@gmail.com",    # Gmail allows no underscore
            "nhanvien+1@gmail.com",   # an alias of someone else's inbox
            "nhanvien@gmail.co",
        )
        with self._client() as client:
            for bad in refused:
                with self.subTest(email=bad):
                    response = self._register(client, email=bad)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("Gmail", response.json()["detail"])
                    auth.register_limiter._hits.clear()

    def test_gmail_addresses_are_accepted_and_stored_lowercased(self):
        with self._client() as client:
            for good in ("Nhan.Vien2024@Gmail.com", "abcdef@gmail.com"):
                with self.subTest(email=good):
                    self.assertEqual(self._register(client, email=good).status_code, 200)
        self.assertIsNotNone(self.users.get_by_email("nhan.vien2024@gmail.com"))

    def test_an_admin_creates_new_accounts_as_gmail_only(self):
        with self._client(self.admin) as admin:
            created = admin.post(
                "/api/users", json={"email": "Kho.HCM@gmail.com", "password": "a good password"},
            )
            refused = admin.post(
                "/api/users", json={"email": "kho@shop.vn", "password": "a good password"},
            )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["email"], "kho.hcm@gmail.com")
        self.assertEqual(refused.status_code, 400)

    def test_an_existing_non_gmail_account_can_still_log_in(self):
        """The Gmail rule is for new accounts only - an admin on the shop's
        own domain, made before it, is not locked out."""
        self.users.create_user("admin2@shop.vn", auth.hash_password("a good password"), is_admin=True)
        with self._client() as client:
            response = client.post(
                "/api/auth/login", json={"email": "admin2@shop.vn", "password": "a good password"},
            )
        self.assertEqual(response.status_code, 200)

    def test_logging_in_needs_an_email(self):
        with self._client() as client:
            response = client.post(
                "/api/auth/login", json={"email": "nhanvien01", "password": "a good password"},
            )
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------------------- approval
    def test_an_admin_approval_makes_the_account_work(self):
        with self._client() as client:
            self._register(client, password="a good password")
        row = self.users.get_by_email("newuser@gmail.com")

        with self._client(self.admin) as admin:
            self.assertEqual(
                admin.post(f"/api/users/{row['id']}/activate").status_code, 200,
            )

        with self._client() as client:
            login = client.post(
                "/api/auth/login",
                json={"email": "newuser@gmail.com", "password": "a good password"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(client.get("/api/auth/me").status_code, 200)

    def test_the_pending_account_is_visible_to_the_admin_list(self):
        """Approval only works if an admin can see there is something to
        approve - the UsersAdmin page filters on exactly these fields."""
        with self._client() as client:
            self._register(client)

        with self._client(self.admin) as admin:
            listed = admin.get("/api/users").json()

        pending = [user for user in listed if user["email"] == "newuser@gmail.com"]
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0]["is_active"])
        self.assertTrue(pending[0]["is_pending"])


@requires_postgres
class CreateAdminPromotionTests(unittest.TestCase):
    def test_promoting_a_pending_signup_also_activates_it(self):
        """Otherwise the bootstrap path produces an admin who cannot log in."""
        schema = TemporaryPostgresSchema()
        self.addCleanup(schema.cleanup)
        store = UserStore(schema.dsn)
        self.addCleanup(store.close)

        pending = store.create_user("boss@x.test", "hash", is_admin=False, is_active=False)

        with mock.patch("dashboard_api.create_admin.UserStore", return_value=store):
            from dashboard_api import create_admin

            # `main()` reads sys.argv directly.
            argv = ["create_admin", "boss@x.test", "a good password"]
            with mock.patch.object(store, "close"), mock.patch("sys.argv", argv):
                create_admin.main()

        row = store.get_by_id(pending["id"])
        self.assertTrue(row["is_admin"])
        self.assertTrue(row["is_active"])


if __name__ == "__main__":
    unittest.main()
