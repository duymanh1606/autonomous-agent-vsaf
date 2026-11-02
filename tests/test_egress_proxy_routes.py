"""The dashboard's egw endpoints: enabling one-IP-per-workspace from the UI.

Same harness as tests/test_workspace_quota.py (temp registry, throwaway user
schema, lifespan stubbed). egw's Admin API is `dashboard_api.egw` patched
with AsyncMocks - its own protocol is covered in test_egress_proxy_admin.py;
here the question is what the routes persist, expose and refuse.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.accounts as accounts_module
import dashboard_api.main as main
from dashboard_api import auth, egw
from dashboard_api.accounts import AccountRegistry
from dashboard_api.users import UserStore
from tests.support import TemporaryPostgresSchema, requires_postgres

_BASE_CONFIG = (
    "account: {}\ncontrol: {}\nbrowser: {}\nextension_bridge: {}\n"
    "paths: {}\nlogging: {}\ngrounding: {}\nllm: {}\nproducts: {}\n"
)


@requires_postgres
class EgwRouteTests(unittest.TestCase):
    def setUp(self):
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

        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.users = UserStore(self.schema.dsn)
        self.addCleanup(self.users.close)

        real_store = main.users_store
        patches = [
            mock.patch.object(main, "registry", self.registry),
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
            mock.patch.object(
                main, "_reader",
                return_value=mock.Mock(health=lambda: {"ok": True, "detail": "stub"}),
            ),
            mock.patch.object(main.usage_store, "totals_by_account", return_value={}),
            mock.patch.dict(main.processes, {}, clear=True),
            # The routes are only reachable in a deployment with the master
            # switch on; write_config reads it for real (not mocked).
            mock.patch.dict(os.environ, {"EGW_ENABLED": "1"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.admin = self.users.create_user("admin@x.test", "hash", is_admin=True)
        self.client = TestClient(main.app)
        self.client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(self.admin["id"]))
        self.account = self.registry.create("shop-a", "Shop A")

    def _patch_egw(self, **overrides):
        defaults = {
            "configured": mock.Mock(return_value=True),
            "list_routes": mock.AsyncMock(return_value=[
                {"id": "route-01", "source_ip": "203.0.113.10", "status": "HEALTHY",
                 "enabled": True, "last_error": None, "held_by": []},
            ]),
            "provision": mock.AsyncMock(return_value={
                "token": "tok-secret", "route": "route-01", "expected_ip": "203.0.113.10",
            }),
            "revoke": mock.AsyncMock(return_value=True),
        }
        defaults.update(overrides)
        for name, value in defaults.items():
            patcher = mock.patch.object(egw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return defaults

    # ------------------------------------------------------------ routes
    def test_routes_hidden_when_egw_is_not_configured(self):
        self._patch_egw(configured=mock.Mock(return_value=False))
        body = self.client.get("/api/egw/routes").json()
        self.assertEqual(body, {"configured": False, "routes": []})

    def test_master_switch_off_hides_routes_even_with_admin_token(self):
        """`configured()` for real this time: EGW_ADMIN_TOKEN set but
        EGW_ENABLED off must read as "feature absent"."""
        with mock.patch.dict(os.environ, {"EGW_ENABLED": "0", "EGW_ADMIN_TOKEN": "x" * 32}):
            body = self.client.get("/api/egw/routes").json()
        self.assertEqual(body, {"configured": False, "routes": []})

    def test_master_switch_off_makes_enable_a_409(self):
        with mock.patch.dict(os.environ, {"EGW_ENABLED": "0", "EGW_ADMIN_TOKEN": "x" * 32}):
            response = self.client.post("/api/accounts/shop-a/egw", json={"route": "route-01"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("EGW_ENABLED", response.json()["detail"])
        self.assertFalse(self.registry.get("shop-a").egw_token)

    def test_routes_are_listed_when_configured(self):
        self._patch_egw()
        body = self.client.get("/api/egw/routes").json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["routes"][0]["id"], "route-01")

    def test_egw_admin_error_becomes_502(self):
        self._patch_egw(list_routes=mock.AsyncMock(side_effect=egw.EgwError("token sai")))
        response = self.client.get("/api/egw/routes")
        self.assertEqual(response.status_code, 502)
        self.assertIn("token sai", response.json()["detail"])

    # ------------------------------------------------------------ enable
    def test_enable_persists_token_route_and_ip_but_hides_the_token(self):
        fakes = self._patch_egw()
        response = self.client.post("/api/accounts/shop-a/egw", json={"route": "route-01"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["has_egw_token"])
        self.assertEqual(body["egw_route"], "route-01")
        self.assertEqual(body["egw_expected_ip"], "203.0.113.10")
        self.assertNotIn("tok-secret", response.text)
        self.assertFalse(body["restart_required"])  # nothing running in this test
        fakes["provision"].assert_awaited_once_with("shop-a", "route-01", current_holder="")

        account = self.registry.get("shop-a")
        self.assertEqual(account.egw_token, "tok-secret")
        import yaml
        data = yaml.safe_load(account.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["egw"], {
            "workspace_id": "shop-a", "proxy_url": accounts_module.DEFAULT_EGW_PROXY_URL,
            "token": "tok-secret", "expected_ip": "203.0.113.10",
        })

    def test_enable_passes_the_current_route_so_reprovision_is_a_rotation(self):
        fakes = self._patch_egw()
        self.registry.update("shop-a", egw_token="old", egw_route="route-01",
                             egw_expected_ip="203.0.113.10")
        self.client.post("/api/accounts/shop-a/egw", json={"route": "route-01"})
        fakes["provision"].assert_awaited_once_with("shop-a", "route-01", current_holder="route-01")

    def test_enable_without_egw_on_this_machine_is_409_not_500(self):
        self._patch_egw(provision=mock.AsyncMock(side_effect=egw.EgwNotConfigured("chưa cài")))
        response = self.client.post("/api/accounts/shop-a/egw", json={"route": "route-01"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.registry.get("shop-a").egw_token)

    def test_enable_on_a_held_route_is_400_and_changes_nothing(self):
        self._patch_egw(provision=mock.AsyncMock(side_effect=egw.EgwError("đang được shop-b dùng")))
        response = self.client.post("/api/accounts/shop-a/egw", json={"route": "route-01"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("shop-b", response.json()["detail"])
        self.assertFalse(self.registry.get("shop-a").egw_token)

    def test_enable_requires_a_route(self):
        self._patch_egw()
        self.assertEqual(
            self.client.post("/api/accounts/shop-a/egw", json={"route": "  "}).status_code, 400,
        )

    # ------------------------------------------------------------ disable
    def test_disable_revokes_and_clears_everything(self):
        fakes = self._patch_egw()
        self.registry.update("shop-a", egw_token="tok", egw_route="route-01",
                             egw_expected_ip="203.0.113.10")
        response = self.client.delete("/api/accounts/shop-a/egw")
        self.assertEqual(response.status_code, 200)
        fakes["revoke"].assert_awaited_once_with("shop-a")
        account = self.registry.get("shop-a")
        self.assertEqual((account.egw_token, account.egw_route, account.egw_expected_ip), ("", "", ""))
        import yaml
        data = yaml.safe_load(account.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("egw", data)

    def test_disable_without_a_token_skips_egw_entirely(self):
        fakes = self._patch_egw()
        self.client.delete("/api/accounts/shop-a/egw")
        fakes["revoke"].assert_not_awaited()

    # ------------------------------------------------------------ hand-paste
    def test_patching_a_pasted_token_clears_route_and_expected_ip(self):
        self.registry.update("shop-a", egw_token="old", egw_route="route-01",
                             egw_expected_ip="203.0.113.10")
        response = self.client.patch("/api/accounts/shop-a", json={"egw_token": "pasted"})
        self.assertEqual(response.status_code, 200)
        account = self.registry.get("shop-a")
        self.assertEqual(account.egw_token, "pasted")
        self.assertEqual((account.egw_route, account.egw_expected_ip), ("", ""))

    def test_patching_other_fields_leaves_egw_alone(self):
        self.registry.update("shop-a", egw_token="keep", egw_route="route-01",
                             egw_expected_ip="203.0.113.10")
        self.client.patch("/api/accounts/shop-a", json={"label": "Renamed"})
        account = self.registry.get("shop-a")
        self.assertEqual((account.egw_token, account.egw_route), ("keep", "route-01"))

    # ------------------------------------------------------------ delete
    def test_deleting_an_egw_workspace_revokes_its_token(self):
        fakes = self._patch_egw()
        self.registry.update("shop-a", egw_token="tok")
        response = self.client.delete("/api/accounts/shop-a")
        self.assertEqual(response.status_code, 200)
        fakes["revoke"].assert_awaited_once_with("shop-a")

    def test_deleting_a_plain_workspace_never_calls_egw(self):
        fakes = self._patch_egw()
        self.client.delete("/api/accounts/shop-a")
        fakes["revoke"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
