"""Per-user workspace quota: `users.max_workspaces` caps how many workspaces
a non-admin may *create*, enforced in `POST /api/accounts`.

The cap is fail-closed, and that is the point worth pinning. An earlier
build read an unset column as "no limit", so every row nobody had thought
about - including a stranger's fresh self-registration - could create
workspaces, and each one spawns a Chrome and an agent process on the host.
NULL now means "an admin has granted nothing", which is 0. There is no way
to express "unlimited" for a non-admin at all.

Two layers, tested separately so a failure points at the right one:

* `UserStore` - the column round-trips, and NULL survives as NULL so the
  admin UI can tell "chưa cấp" from a limit deliberately set to 0.
* The routes - an unset quota creates nothing; the cap counts *owned*
  workspaces (created by the user), not granted ones; admins are never
  capped; the check-then-act cannot be raced past; the admin-only PATCH
  validates; and lowering a limit below what a user already owns only stops
  further creation, it never deletes.

Credential-free: Chrome, the agent and the LLM are never touched. The
registry is redirected to a temp directory (same reason as
tests/test_account_provisioning.py - `create()` otherwise writes into the
real configs/accounts/), and the user store gets a throwaway schema.
"""

import sys
import tempfile
import threading
import time
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
class UserStoreQuotaTests(unittest.TestCase):
    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.store = UserStore(self.schema.dsn)
        self.addCleanup(self.store.close)

    def test_a_new_user_starts_with_no_quota_granted(self):
        """NULL, not a number - and `workspace_limit` reads that as 0. A
        signup nobody has approved must not be able to create anything."""
        row = self.store.create_user("a@x.test", "hash", is_admin=False)
        self.assertIsNone(row["max_workspaces"])
        self.assertIsNone(self.store.get_by_id(row["id"])["max_workspaces"])
        self.assertEqual(main.workspace_limit(row), 0)

    def test_limit_round_trips_and_null_clears_it(self):
        row = self.store.create_user("b@x.test", "hash", is_admin=False)
        self.store.set_max_workspaces(row["id"], 3)
        self.assertEqual(self.store.get_by_id(row["id"])["max_workspaces"], 3)
        self.store.set_max_workspaces(row["id"], 0)
        self.assertEqual(self.store.get_by_id(row["id"])["max_workspaces"], 0)
        # NULL is preserved rather than coerced, so the admin UI can show
        # "chưa cấp" differently from a deliberate 0. Both decide the same.
        self.store.set_max_workspaces(row["id"], None)
        self.assertIsNone(self.store.get_by_id(row["id"])["max_workspaces"])

    def test_workspace_limit_reads_an_absent_quota_as_zero(self):
        """The single place NULL becomes a number. If this ever flips back
        to "unlimited", every unreviewed account can spawn processes."""
        self.assertEqual(main.workspace_limit({}), 0)
        self.assertEqual(main.workspace_limit({"max_workspaces": None}), 0)
        self.assertEqual(main.workspace_limit({"max_workspaces": 0}), 0)
        self.assertEqual(main.workspace_limit({"max_workspaces": 4}), 4)

    def test_list_users_carries_the_limit(self):
        row = self.store.create_user("c@x.test", "hash", is_admin=False)
        self.store.set_max_workspaces(row["id"], 2)
        listed = {user["id"]: user for user in self.store.list_users()}
        self.assertEqual(listed[row["id"]]["max_workspaces"], 2)

    def test_public_user_exposes_the_limit_to_the_session(self):
        """The middleware builds the per-request user from this; the route
        reads the cap from there, so it has to survive the projection."""
        row = self.store.create_user("d@x.test", "hash", is_admin=False)
        self.store.set_max_workspaces(row["id"], 5)
        public = auth.public_user(self.store.get_by_id(row["id"]), [])
        self.assertEqual(public["max_workspaces"], 5)


@requires_postgres
class WorkspaceQuotaRouteTests(unittest.TestCase):
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

    # ---------------------------------------------------------------- cases
    def test_an_ungranted_quota_creates_nothing(self):
        """The regression this whole file exists for: a user an admin has
        not given a quota is refused, and nothing is written to disk."""
        client = self._client(self.user)

        response = self._create(client, "shop lén")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.users.accessible_account_ids(self.user["id"]), [])
        self.assertEqual(list((Path(self.tmp.name) / "accounts").glob("*.yaml")), [])

    def test_a_granted_quota_lets_the_user_create_and_own_it(self):
        self.users.set_max_workspaces(self.user["id"], 2)
        client = self._client(self.user)

        response = self._create(client, "Shop Của Tôi")

        self.assertEqual(response.status_code, 200)
        created = self.registry.get(response.json()["id"])
        self.assertEqual(created.owner_id, self.user["id"])
        # Self-granted, or their own new workspace would be invisible to
        # them: a non-admin's list is filtered by user_account_access.
        self.assertEqual(
            self.users.accessible_account_ids(self.user["id"]), [created.id],
        )
        listed = [item["id"] for item in client.get("/api/accounts").json()]
        self.assertEqual(listed, [created.id])

    def test_creation_is_refused_at_the_limit_and_nothing_is_written(self):
        self.users.set_max_workspaces(self.user["id"], 1)
        client = self._client(self.user)

        self.assertEqual(self._create(client, "first").status_code, 200)
        refused = self._create(client, "second")

        self.assertEqual(refused.status_code, 403)
        self.assertIn("tối đa 1 workspace", refused.json()["detail"])
        self.assertIn("đang có 1", refused.json()["detail"])
        # Refused before registry.create: no YAML, no registry row, no grant.
        self.assertEqual(len(self._owned(self.user["id"])), 1)
        self.assertEqual(len(self.users.accessible_account_ids(self.user["id"])), 1)
        yaml_files = list((Path(self.tmp.name) / "accounts").glob("*.yaml"))
        self.assertEqual(len(yaml_files), 1)

    def test_concurrent_creation_cannot_exceed_the_limit(self):
        """The check-then-act (read owned count, then create) is guarded by
        registry.lock end-to-end in create_account, so two racing requests
        cannot both read the same pre-write count and both slip past the cap.

        `registry.list` takes no lock of its own, so without that wrapper
        this is a live TOCTOU. The sleep is what makes the test mean
        something: the natural window between the count and the write is a
        few microseconds wide, so eight threads racing it pass with or
        without the lock. Widening it to 50ms makes the unguarded version
        fail every time (all eight read 0 and all eight create), which is
        the only way this test can defend the lock it describes.
        """
        self.users.set_max_workspaces(self.user["id"], 1)
        real_count = main._owned_workspace_count

        def _slow_count(user_id):
            owned = real_count(user_id)
            time.sleep(0.05)
            return owned

        results: list[int] = []

        def _attempt(label):
            client = self._client(self.user)
            results.append(self._create(client, label).status_code)

        with mock.patch.object(main, "_owned_workspace_count", _slow_count):
            threads = [
                threading.Thread(target=_attempt, args=(f"race-{i}",))
                for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(403), 7)
        self.assertEqual(len(self._owned(self.user["id"])), 1)

    def test_zero_forbids_creating_any(self):
        self.users.set_max_workspaces(self.user["id"], 0)
        self.assertEqual(self._create(self._client(self.user), "x").status_code, 403)
        self.assertEqual(self._owned(self.user["id"]), [])

    def test_admins_are_never_capped(self):
        self.users.set_max_workspaces(self.admin["id"], 0)
        client = self._client(self.admin)
        self.assertEqual(self._create(client, "a").status_code, 200)
        self.assertEqual(self._create(client, "b").status_code, 200)

    def test_granted_workspaces_do_not_count_against_the_owner_limit(self):
        """The cap is on creating. A workspace someone else made and merely
        shared with this user is not theirs (they cannot even delete it)."""
        self.users.set_max_workspaces(self.user["id"], 1)
        shared = self.registry.create("", "admin's shop", owner_id=self.admin["id"])
        self.users.grant_access(self.user["id"], shared.id)

        client = self._client(self.user)
        # Sees two workspaces, but has created none - still allowed one.
        self.assertEqual(self._create(client, "mine").status_code, 200)
        self.assertEqual(self._create(client, "one too many").status_code, 403)
        self.assertEqual(len(self._owned(self.user["id"])), 1)

    def test_a_raised_limit_takes_effect_on_the_next_request(self):
        """No re-login: the middleware re-reads the users row per request."""
        self.users.set_max_workspaces(self.user["id"], 1)
        client = self._client(self.user)
        self.assertEqual(self._create(client, "one").status_code, 200)
        self.assertEqual(self._create(client, "two").status_code, 403)

        self._client(self.admin).patch(
            f"/api/users/{self.user['id']}/max-workspaces", json={"max_workspaces": 2},
        )
        self.assertEqual(self._create(client, "two").status_code, 200)

    def test_lowering_below_what_is_owned_stops_creation_but_deletes_nothing(self):
        self.users.set_max_workspaces(self.user["id"], 5)
        client = self._client(self.user)
        self.assertEqual(self._create(client, "one").status_code, 200)
        self.assertEqual(self._create(client, "two").status_code, 200)

        self.users.set_max_workspaces(self.user["id"], 1)

        self.assertEqual(self._create(client, "three").status_code, 403)
        self.assertEqual(len(self._owned(self.user["id"])), 2)
        listed = client.get("/api/accounts").json()
        self.assertEqual(len(listed), 2)

    # --------------------------------------------------- the admin endpoint
    def test_patch_is_admin_only(self):
        response = self._client(self.user).patch(
            f"/api/users/{self.user['id']}/max-workspaces", json={"max_workspaces": 9},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.users.get_by_id(self.user["id"])["max_workspaces"])

    def test_patch_validates(self):
        admin = self._client(self.admin)
        self.assertEqual(
            admin.patch(f"/api/users/{self.user['id']}/max-workspaces",
                        json={"max_workspaces": -1}).status_code,
            400,
        )
        self.assertEqual(
            admin.patch("/api/users/999999/max-workspaces",
                        json={"max_workspaces": 1}).status_code,
            404,
        )

    def test_patch_null_clears_the_quota_rather_than_lifting_it(self):
        """null is "take the grant away", not "make it unlimited" - the
        difference between fail-closed and the hole this replaced."""
        self.users.set_max_workspaces(self.user["id"], 2)
        admin = self._client(self.admin)

        response = admin.patch(
            f"/api/users/{self.user['id']}/max-workspaces", json={"max_workspaces": None},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.users.get_by_id(self.user["id"])["max_workspaces"])
        self.assertEqual(self._create(self._client(self.user), "free").status_code, 403)

    def test_list_users_reports_owned_count_and_limit(self):
        self.users.set_max_workspaces(self.user["id"], 4)
        client = self._client(self.user)
        self._create(client, "one")
        self._create(client, "two")

        rows = {row["id"]: row for row in self._client(self.admin).get("/api/users").json()}
        self.assertEqual(rows[self.user["id"]]["owned_workspaces"], 2)
        self.assertEqual(rows[self.user["id"]]["max_workspaces"], 4)
        self.assertEqual(rows[self.admin["id"]]["owned_workspaces"], 0)

    def test_me_carries_the_limit_for_the_sidebar(self):
        self.users.set_max_workspaces(self.user["id"], 2)
        me = self._client(self.user).get("/api/auth/me").json()
        self.assertEqual(me["max_workspaces"], 2)


if __name__ == "__main__":
    unittest.main()
