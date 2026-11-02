"""Who may create a workspace, and how everyone else gets into one.

Creating a workspace is not an ordinary user action: it writes a YAML config
and a registry entry, and the very next thing anyone does with it (`POST
.../start`) spawns a Chrome and an agent process on the host. So it takes
either an admin or a quota an admin granted (`users.max_workspaces`, which
is 0 until then). The quota itself is tested in tests/test_workspace_quota.py;
what this file pins is the default - nobody creates anything unasked - and
the granting path staff actually use.

Two kinds of permission are deliberately different here and both are pinned
below: *access* (granted, lets you use a workspace) and *ownership* (created
it, lets you rename or delete it - `_owned_account_or_404`).

Credential-free: Chrome, the agent and the LLM are never touched. The
registry is redirected to a temp directory (same reason as
tests/test_account_provisioning.py - `create()` otherwise writes into the
real configs/accounts/), and the user store gets a throwaway schema.
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
class WorkspaceAccessRouteTests(unittest.TestCase):
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

    # -------------------------------------------------------------- helpers
    def _client(self, user_row) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(user_row["id"]))
        return client

    def _create(self, client, label):
        return client.post("/api/accounts", json={"label": label})

    def _owned(self, user_id):
        return [a for a in self.registry.list() if a.owner_id == user_id]

    # ------------------------------------------------------- creating
    def test_a_non_admin_with_no_quota_cannot_create_a_workspace(self):
        """The default for everyone who is not an admin, including a fresh
        self-registration: `max_workspaces` is unset, which reads as 0."""
        response = self._create(self._client(self.user), "shop lén")

        self.assertEqual(response.status_code, 403)
        # Refused before registry.create: no YAML, no registry row, no grant.
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.users.accessible_account_ids(self.user["id"]), [])
        self.assertEqual(list((Path(self.tmp.name) / "accounts").glob("*.yaml")), [])

    def test_an_admin_creates_one_and_owns_it(self):
        response = self._create(self._client(self.admin), "Shop Chính")

        self.assertEqual(response.status_code, 200)
        created = self.registry.get(response.json()["id"])
        self.assertEqual(created.owner_id, self.admin["id"])

    def test_an_admin_needs_no_grant_to_see_their_own(self):
        """Admins see every workspace unconditionally, so the self-grant a
        non-admin creator gets would be dead weight here."""
        client = self._client(self.admin)
        account_id = self._create(client, "Shop Chính").json()["id"]

        self.assertEqual(self.users.accessible_account_ids(self.admin["id"]), [])
        listed = [item["id"] for item in client.get("/api/accounts").json()]
        self.assertEqual(listed, [account_id])

    # -------------------------------------------------------- granting
    def test_a_workspace_is_invisible_until_it_is_granted(self):
        account_id = self._create(self._client(self.admin), "Shop Chính").json()["id"]
        staff = self._client(self.user)

        self.assertEqual(staff.get("/api/accounts").json(), [])
        self.assertEqual(staff.get(f"/api/accounts/{account_id}/status").status_code, 403)

        granted = self._client(self.admin).post(
            f"/api/users/{self.user['id']}/access", json={"account_id": account_id},
        )
        self.assertEqual(granted.status_code, 200)

        self.assertEqual(
            [item["id"] for item in staff.get("/api/accounts").json()], [account_id],
        )

    def test_revoking_takes_the_workspace_away_again(self):
        account_id = self._create(self._client(self.admin), "Shop Chính").json()["id"]
        admin = self._client(self.admin)
        admin.post(f"/api/users/{self.user['id']}/access", json={"account_id": account_id})
        staff = self._client(self.user)
        self.assertEqual(len(staff.get("/api/accounts").json()), 1)

        admin.delete(f"/api/users/{self.user['id']}/access/{account_id}")

        self.assertEqual(staff.get("/api/accounts").json(), [])

    def test_only_an_admin_may_grant(self):
        account_id = self._create(self._client(self.admin), "Shop Chính").json()["id"]

        response = self._client(self.user).post(
            f"/api/users/{self.user['id']}/access", json={"account_id": account_id},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.users.accessible_account_ids(self.user["id"]), [])

    def test_granting_an_unknown_workspace_is_a_404(self):
        response = self._client(self.admin).post(
            f"/api/users/{self.user['id']}/access", json={"account_id": "khong-co"},
        )
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------- access vs ownership
    def test_a_granted_user_may_use_but_not_rename_or_delete(self):
        """Access is not ownership: staff run the workspace, the admin who
        created it is the one who can change or remove it."""
        account_id = self._create(self._client(self.admin), "Shop Chính").json()["id"]
        self._client(self.admin).post(
            f"/api/users/{self.user['id']}/access", json={"account_id": account_id},
        )
        staff = self._client(self.user)

        # Not 403: the permission gate let it through. (It answers 503 here
        # because no agent is actually running in this test, which is a
        # different question entirely.)
        self.assertNotEqual(
            staff.get(f"/api/accounts/{account_id}/status").status_code, 403,
        )
        self.assertEqual(
            staff.patch(f"/api/accounts/{account_id}", json={"label": "Đổi tên"}).status_code,
            403,
        )
        self.assertEqual(staff.delete(f"/api/accounts/{account_id}").status_code, 403)
        self.assertEqual(self.registry.get(account_id).label, "Shop Chính")

    # ------------------------------------------- admin-created users (POST)
    def test_admin_creates_a_plain_user(self):
        response = self._client(self.admin).post(
            "/api/users", json={"email": "NewStaff@gmail.com ".strip(), "password": "longenoughpass"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["is_admin"])
        self.assertTrue(body["is_active"])
        self.assertEqual(body["owned_workspaces"], 0)
        row = self.users.get_by_email("newstaff@gmail.com")
        self.assertIsNotNone(row)
        self.assertFalse(row["is_admin"])

    def test_created_user_can_log_in_with_the_chosen_password(self):
        self._client(self.admin).post(
            "/api/users", json={"email": "newstaff@gmail.com", "password": "longenoughpass"},
        )
        row = self.users.get_by_email("newstaff@gmail.com")
        self.assertTrue(auth.verify_password("longenoughpass", row["password_hash"]))

    def test_post_users_is_admin_only(self):
        response = self._client(self.user).post(
            "/api/users", json={"email": "sneakystaff@gmail.com", "password": "longenoughpass"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.users.get_by_email("sneakystaff@gmail.com"))

    def test_post_users_validates_account_name_and_password(self):
        admin = self._client(self.admin)
        self.assertEqual(
            admin.post("/api/users", json={"email": "có dấu", "password": "longenoughpass"}).status_code,
            400,
        )
        self.assertEqual(
            admin.post("/api/users", json={"email": "x@y.test", "password": "short"}).status_code,
            400,
        )

    def test_post_users_refuses_a_duplicate_email(self):
        admin = self._client(self.admin)
        first = admin.post("/api/users", json={"email": "dupstaff@gmail.com", "password": "longenoughpass"})
        self.assertEqual(first.status_code, 200)
        second = admin.post("/api/users", json={"email": "dupstaff@gmail.com", "password": "anotherpassword"})
        self.assertEqual(second.status_code, 409)

    def test_created_user_appears_in_list_users_with_zero_usage(self):
        self._client(self.admin).post(
            "/api/users", json={"email": "newstaff@gmail.com", "password": "longenoughpass"},
        )
        rows = {row["email"]: row for row in self._client(self.admin).get("/api/users").json()}
        self.assertEqual(rows["newstaff@gmail.com"]["usage"]["total_tokens"], 0)
        self.assertEqual(rows["newstaff@gmail.com"]["owned_workspaces"], 0)


if __name__ == "__main__":
    unittest.main()
