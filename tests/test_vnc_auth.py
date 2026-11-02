"""The noVNC gate: `GET /api/vnc-auth`, which nginx calls via `auth_request`
before proxying anything under `/vnc/<account_id>/`.

Why this endpoint exists: a workspace's screen used to sit outside every
permission the rest of the dashboard has. websockify binds 127.0.0.1, so
*reaching the port was the authorization* - which meant anybody who could
reach loopback (an `ssh -L`, a tailnet) saw every workspace's screen, granted
or not. Routing it through `_account_or_404` puts the screen under the same
rule as every other account-scoped route.

What nginx does with each answer, and therefore what these tests pin:

    2xx              -> proxy, using the port from X-Novnc-Port
    401 / 403        -> refused with that status
    anything else    -> nginx turns it into a 500

So a missing or wrong answer can only ever fail closed. The port travelling
back as a response header is what lets a workspace created after nginx
started work with no config change.

Credential-free: no Chrome, no agent, no LLM, no real nginx. The registry is
redirected to a temp directory and the user store gets a throwaway schema,
same setup as tests/test_workspace_access.py.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.accounts as accounts_module
import dashboard_api.main as main
from dashboard_api import auth
from dashboard_api.accounts import AccountRegistry
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres


# Small enough to be readable, big enough for write_config's overrides.
_BASE_CONFIG = (
    "account: {}\ncontrol: {}\nbrowser: {}\nextension_bridge: {}\n"
    "paths: {}\nlogging: {}\ngrounding: {}\nllm: {}\nproducts: {}\n"
)


@requires_postgres
class VncAuthRouteTests(unittest.TestCase):
    def setUp(self):
        # --- registry in a temp dir, never the real configs/accounts/ ---
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        accounts_dir = Path(self.tmp.name) / "accounts"
        for target, value in (
            ("ACCOUNTS_DIR", accounts_dir),
            ("REGISTRY_PATH", accounts_dir / "accounts.json"),
        ):
            patcher = mock.patch.object(accounts_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        base_config = Path(self.tmp.name) / "config.yaml"
        base_config.write_text(_BASE_CONFIG, encoding="utf-8")
        self.registry = AccountRegistry(base_config_path=base_config)

        # --- users on a throwaway schema ---
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.users = UserStore(self.schema.dsn)
        self.addCleanup(self.users.close)

        # `auth.install_middleware(app, users_store)` closed over the store
        # object that existed at import time, so swapping `main.users_store`
        # alone would leave the middleware resolving session cookies against
        # the real database. Redirect that original object's two lookups to
        # the throwaway store instead.
        real_store = main.users_store
        patches = [
            mock.patch.object(main, "registry", self.registry),
            mock.patch.object(main, "users_store", self.users),
            mock.patch.object(real_store, "get_by_id", side_effect=self.users.get_by_id),
            mock.patch.object(
                real_store, "accessible_account_ids",
                side_effect=self.users.accessible_account_ids,
            ),
            # Lifespan side effects are not under test and must not touch
            # the real machine (same set as test_events_websocket_shutdown).
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            # list_accounts pings each workspace's database; a stub keeps
            # the test off the network and fast.
            mock.patch.object(
                main, "_reader",
                return_value=mock.Mock(health=lambda: {"ok": True, "detail": "stub"}),
            ),
            # Token usage per user is read from the LLM ledger - not under test.
            mock.patch.object(main.usage_store, "totals_by_account", return_value={}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.admin = self.users.create_user("admin@x.test", "hash", is_admin=True)
        self.user = self.users.create_user("op@x.test", "hash", is_admin=False)
        # A second non-admin, so "granted" and "not granted" can be shown
        # against the same workspace in one test.
        self.other = self.users.create_user("khac@x.test", "hash", is_admin=False)

    # -------------------------------------------------------------- helpers
    def _client(self, user_row) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(user_row["id"]))
        return client

    def _ask(self, client, uri: str):
        """What nginx's subrequest looks like: a GET to the fixed auth path,
        with the real path carried over in a header."""
        return client.get("/api/vnc-auth", headers={"X-Original-URI": uri})

    def _make_workspace(self) -> str:
        return self.registry.create("", "Shop Chính", owner_id=self.admin["id"]).id

    # ------------------------------------------------------------- allowed
    def test_the_owner_is_allowed_and_gets_the_port(self):
        account_id = self._make_workspace()
        expected = self.registry.get(account_id).novnc_port

        response = self._ask(self._client(self.admin), f"/vnc/{account_id}/vnc.html")

        self.assertEqual(response.status_code, 200)
        # The header is the contract with nginx: without it $vnc_port is
        # empty and proxy_pass has nowhere to go.
        self.assertEqual(response.headers["X-Novnc-Port"], str(expected))

    def test_a_granted_user_is_allowed(self):
        account_id = self._make_workspace()
        self.users.grant_access(self.user["id"], account_id)

        response = self._ask(self._client(self.user), f"/vnc/{account_id}/vnc.html")

        self.assertEqual(response.status_code, 200)

    def test_the_websocket_upgrade_path_is_authorized_the_same_way(self):
        """auth_request runs per request, so the stream itself is checked -
        not just the page that opens it. Anyone who skipped straight to
        /websockify would otherwise bypass the gate entirely."""
        account_id = self._make_workspace()
        self.users.grant_access(self.user["id"], account_id)

        allowed = self._ask(self._client(self.user), f"/vnc/{account_id}/websockify")
        self.assertEqual(allowed.status_code, 200)

        refused = self._ask(self._client(self.other), f"/vnc/{account_id}/websockify")
        self.assertEqual(refused.status_code, 403)

    # ------------------------------------------------------------- refused
    def test_a_user_without_a_grant_is_refused(self):
        """The whole point. This user can log in and use the dashboard; they
        were simply never given this workspace."""
        account_id = self._make_workspace()

        response = self._ask(self._client(self.user), f"/vnc/{account_id}/vnc.html")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("X-Novnc-Port", response.headers)

    def test_a_revoked_grant_closes_the_screen_on_the_next_request(self):
        """No separate VNC permission to forget to revoke: it reads the same
        grant table as everything else, per request."""
        account_id = self._make_workspace()
        self.users.grant_access(self.user["id"], account_id)
        client = self._client(self.user)
        self.assertEqual(self._ask(client, f"/vnc/{account_id}/vnc.html").status_code, 200)

        self.users.revoke_access(self.user["id"], account_id)

        self.assertEqual(self._ask(client, f"/vnc/{account_id}/vnc.html").status_code, 403)

    def test_no_session_is_a_401(self):
        response = TestClient(main.app).get(
            "/api/vnc-auth", headers={"X-Original-URI": "/vnc/shop-any/vnc.html"},
        )
        self.assertEqual(response.status_code, 401)

    def test_an_unknown_workspace_is_a_404(self):
        response = self._ask(self._client(self.admin), "/vnc/khong-co/vnc.html")
        self.assertEqual(response.status_code, 404)

    # -------------------------------------------------- malformed / direct
    def test_a_uri_that_is_not_a_vnc_path_is_refused(self):
        """nginx only ever sends /vnc/<id>/... here, so anything else is a
        misconfiguration or a direct call - and must not fall through to a
        default workspace."""
        client = self._client(self.admin)
        for uri in ("", "/", "/api/accounts", "/vnc/", "/vncshop/x", "/vnc//vnc.html"):
            with self.subTest(uri=uri):
                self.assertEqual(self._ask(client, uri).status_code, 403)

    def test_a_traversal_attempt_does_not_reach_another_workspace(self):
        """The id pattern has no dots-and-slashes in it, so `..` cannot be
        used to walk from a granted workspace to one that is not."""
        granted = self._make_workspace()
        self.users.grant_access(self.user["id"], granted)
        secret = self._make_workspace()

        response = self._ask(
            self._client(self.user), f"/vnc/{granted}/../{secret}/vnc.html",
        )

        # Either refused outright, or still resolved to the granted
        # workspace - never to `secret`.
        if response.status_code == 200:
            self.assertEqual(
                response.headers["X-Novnc-Port"],
                str(self.registry.get(granted).novnc_port),
            )
        else:
            self.assertIn(response.status_code, (403, 404))


if __name__ == "__main__":
    unittest.main()
