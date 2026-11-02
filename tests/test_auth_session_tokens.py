"""Session tokens that expire, and that can be taken away.

Before this, a session token was `{"sub": "7"}` - no `iat`, no `exp` - signed
with a key that normally lives forever in `.env`. It was valid until the end
of time, and `logout` only deleted the browser's copy of it. Changing a
password, resetting a password, even deactivating the account did nothing to a
cookie someone else had already copied.

The fix is two halves that only work together: every token now carries when it
was minted, and every user carries a watermark saying which mint times are
still acceptable. The ordering invariant between them (bump the watermark
first, mint the replacement after) is the subtle part, and has its own test.
"""

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth, security_config
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres


class TokenShapeTests(unittest.TestCase):
    def test_a_token_says_when_it_was_minted_and_when_it_dies(self):
        before = int(time.time())
        payload = jwt.decode(
            auth.create_session_token(7), auth.SECRET_KEY,
            algorithms=[auth.JWT_ALGORITHM],
        )

        self.assertEqual(payload["sub"], "7")
        self.assertGreaterEqual(payload["iat"], before)
        self.assertEqual(
            payload["exp"] - payload["iat"], security_config.session_ttl_seconds(),
        )

    def test_the_ttl_is_a_week_not_a_month(self):
        self.assertEqual(security_config.DEFAULT_SESSION_TTL_SECONDS, 7 * 24 * 3600)

    def test_an_expired_token_does_not_decode(self):
        expired = jwt.encode(
            {"sub": "7", "iat": int(time.time()) - 7200,
             "exp": int(time.time()) - 3600},
            auth.SECRET_KEY, algorithm=auth.JWT_ALGORITHM,
        )
        self.assertIsNone(auth.decode_session(expired))

    def test_a_legacy_token_without_iat_is_refused(self):
        """The old shape. It has to stop working, or the deploy that adds
        expiry leaves every already-issued eternal cookie alive."""
        legacy = jwt.encode(
            {"sub": "7"}, auth.SECRET_KEY, algorithm=auth.JWT_ALGORITHM,
        )
        self.assertIsNone(auth.decode_session(legacy))
        self.assertIsNone(auth.decode_session_token(legacy))

    def test_a_token_signed_with_another_key_is_refused(self):
        foreign = jwt.encode(
            {"sub": "7", "iat": int(time.time()), "exp": int(time.time()) + 60},
            "a" * 40, algorithm="HS256",   # long enough not to warn
        
        )
        self.assertIsNone(auth.decode_session(foreign))


class RevocationComparisonTests(unittest.TestCase):
    @staticmethod
    def _row(valid_from_offset_seconds: float):
        return {
            "id": 1, "is_active": True,
            "sessions_valid_from":
                datetime.now(timezone.utc) + timedelta(seconds=valid_from_offset_seconds),
        }

    def test_a_token_minted_before_the_watermark_is_revoked(self):
        self.assertTrue(auth.session_revoked(self._row(+60), int(time.time())))

    def test_a_token_minted_after_the_watermark_survives(self):
        self.assertFalse(auth.session_revoked(self._row(-60), int(time.time())))

    def test_there_is_no_grace_period(self):
        """Tolerance here would mean 'a token minted just before the password
        change survives it' - i.e. exactly the hole this closes."""
        now = int(time.time())
        row = {"id": 1, "sessions_valid_from": datetime.fromtimestamp(now, timezone.utc)}

        self.assertTrue(auth.session_revoked(row, now - 1))
        self.assertFalse(auth.session_revoked(row, now))

    def test_a_row_without_the_column_is_not_revoked(self):
        """Several existing tests patch `get_by_id` with a minimal row and
        have nothing to do with revocation."""
        self.assertFalse(auth.session_revoked({"id": 1, "is_active": True}, 123))
        self.assertFalse(auth.session_revoked(None, 123))


@requires_postgres
class SessionRevocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.store = UserStore(self.schema.dsn)
        self.addCleanup(self.store.close)
        self.user = self.store.create_user("op@x.test", "hash", is_admin=False)

    def test_a_new_row_has_a_timezone_aware_watermark(self):
        """A naive datetime would silently mis-rank by the UTC offset."""
        valid_from = self.store.get_by_id(self.user["id"])["sessions_valid_from"]

        self.assertIsNotNone(valid_from.tzinfo)
        self.assertIsNotNone(valid_from.utcoffset())

    def test_invalidating_moves_the_watermark_forward(self):
        before = self.store.get_by_id(self.user["id"])["sessions_valid_from"]
        self.store.invalidate_sessions(self.user["id"])
        after = self.store.get_by_id(self.user["id"])["sessions_valid_from"]

        self.assertGreater(after, before)

    def test_the_watermark_never_moves_backwards(self):
        """`GREATEST` keeps it monotonic, so two concurrent invalidations
        cannot revive a revoked token."""
        self.store.invalidate_sessions(self.user["id"])
        high = self.store.get_by_id(self.user["id"])["sessions_valid_from"]

        self.store.invalidate_sessions(
            self.user["id"], at=high - timedelta(hours=1),
        )

        self.assertEqual(self.store.get_by_id(self.user["id"])["sessions_valid_from"], high)


@requires_postgres
class SessionRevocationRouteTests(unittest.TestCase):
    """End to end: a cookie in hand, and what stops it working."""

    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.users = UserStore(self.schema.dsn)
        self.addCleanup(self.users.close)

        # The middleware closed over the store object that existed at import
        # time, so redirecting `main.users_store` alone would leave it
        # resolving cookies against the real database.
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

        self.admin = self.users.create_user("admin@x.test", "hash", is_admin=True)
        self.user = self.users.create_user(
            "op@x.test", auth.hash_password("correct horse"), is_admin=False,
        )

    def _client(self, user_row) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(
            auth.SESSION_COOKIE, auth.create_session_token(user_row["id"]),
        )
        return client

    def test_a_fresh_cookie_works(self):
        with self._client(self.user) as client:
            self.assertEqual(client.get("/api/auth/me").status_code, 200)

    def test_a_reset_password_kills_sessions_already_handed_out(self):
        client = self._client(self.user)
        with client:
            self.assertEqual(client.get("/api/auth/me").status_code, 200)

            token = self.users.create_reset_token(self.user["id"])
            self.assertEqual(
                client.post(
                    "/api/auth/reset-password",
                    json={"token": token, "password": "a new password"},
                ).status_code, 200,
            )

            self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_an_admin_setting_a_password_kills_them_too(self):
        victim = self._client(self.user)
        admin = self._client(self.admin)
        with victim, admin:
            self.assertEqual(victim.get("/api/auth/me").status_code, 200)

            admin.post(
                f"/api/users/{self.user['id']}/set-password",
                json={"password": "brand new password"},
            )

            self.assertEqual(victim.get("/api/auth/me").status_code, 401)

    def test_deactivating_kills_them_and_reactivating_does_not_bring_them_back(self):
        victim = self._client(self.user)
        admin = self._client(self.admin)
        with victim, admin:
            admin.post(f"/api/users/{self.user['id']}/deactivate")
            self.assertEqual(victim.get("/api/auth/me").status_code, 401)

            admin.post(f"/api/users/{self.user['id']}/activate")

            # Reactivation restores the account, not the old cookie.
            self.assertEqual(victim.get("/api/auth/me").status_code, 401)

    def test_a_replacement_cookie_minted_after_the_revocation_works(self):
        """The ordering invariant: revoke first, mint second. If a caller ever
        reverses it, the freshly issued cookie is dead on arrival - which is
        why there is no clock-skew tolerance to paper over it."""
        self.users.invalidate_sessions(self.user["id"])
        # A login is exactly that sequence, from the user's side.
        with TestClient(main.app) as client:
            response = client.post(
                "/api/auth/login",
                json={"email": "op@x.test", "password": "correct horse"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/auth/me").status_code, 200)

    def test_logging_out_does_not_end_other_devices(self):
        """Documented single-device semantics: `sessions_valid_from` is
        per-user, so logging out on a phone would kill the desktop too."""
        phone = self._client(self.user)
        desktop = self._client(self.user)
        with phone, desktop:
            phone.post("/api/auth/logout")

            self.assertEqual(desktop.get("/api/auth/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
