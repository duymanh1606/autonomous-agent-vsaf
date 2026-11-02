"""The order and product routes, over real HTTP, after moving out of main.py.

They now live in dashboard_api/routes/ as APIRouters. Nothing else in the
suite requests them through the app: the product tests call the handler
functions directly, and an unauthenticated request is answered 401 by the
auth middleware before routing - so a router that was never included would
still leave every other test green. These requests go through the whole
stack as a signed-in user, and check that the per-account access check
still stands in front of them.
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
from dashboard_api.orders import OrderStore
from dashboard_api.routes import orders as order_routes
from dashboard_api.users import UserStore
from storage.product_repository import ProductRepository
from tests.support import TemporaryPostgresSchema, requires_postgres


_BASE_CONFIG = (
    "account: {}\ncontrol: {}\nbrowser: {}\n"
    "paths: {}\nlogging: {}\ngrounding: {}\nllm: {}\nproducts: {}\n"
)


class DashboardRouteFixture(unittest.TestCase):
    """A signed-in admin, one workspace, a real order store and catalogue.
    Shared with other HTTP-level tests (tests/test_order_stock.py)."""

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
        self.orders = OrderStore(self.schema.dsn)
        self.catalogue = ProductRepository(product_root=Path(self.tmp.name) / "products")

        # Same reason as tests/test_workspace_access.py: the auth middleware
        # closed over the store that existed at import time.
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
            mock.patch.object(main.usage_store, "totals_by_account", return_value={}),
            mock.patch.object(
                main, "_reader",
                return_value=mock.Mock(customer_identity=lambda _cid: {}),
            ),
            # What the routes reach through main at call time - patched there,
            # which is the point of looking them up late.
            mock.patch.object(main, "orders_store", self.orders),
            mock.patch.object(main, "_products", lambda account: self.catalogue),
            # The sheet export is a network call, not under test.
            mock.patch.object(order_routes, "export_order"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.admin = self.users.create_user("admin@x.test", "hash", is_admin=True)
        self.stranger = self.users.create_user("other@x.test", "hash", is_admin=False)
        self.client = self._client(self.admin)
        created = self.client.post("/api/accounts", json={"label": "shop"})
        self.assertEqual(created.status_code, 200, created.text)
        self.account_id = created.json()["id"]

    def _client(self, user_row) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(user_row["id"]))
        return client


@requires_postgres
class MovedRoutesTests(DashboardRouteFixture):

    def test_products_are_served_end_to_end(self):
        base = f"/api/accounts/{self.account_id}/products"

        created = self.client.post(
            base, json={"product_id": "sp1", "fields": {
                "name": "Sữa tắm", "category": "Tắm", "price": 90000, "stock": 3,
            }},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.get(f"{base}/sp1").json()["name"], "Sữa tắm")
        updated = self.client.put(f"{base}/sp1", json={"stock": 5})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["stock"], 5)
        listed = self.client.get(base).json()["products"]
        self.assertIn("sp1", [row["id"] for row in listed])
        # The validation that moved with the handlers still runs.
        self.assertEqual(self.client.put(f"{base}/sp1", json={"stock": -1}).status_code, 400)
        self.assertEqual(self.client.delete(f"{base}/sp1").status_code, 200)
        self.assertEqual(self.client.get(f"{base}/sp1").status_code, 404)

    def test_orders_are_served_end_to_end(self):
        base = f"/api/accounts/{self.account_id}/orders"

        created = self.client.post(base, json={
            "customer_name": "Chị Lan", "items": "2x Milo",
            "total_amount": 560000, "shipping_address": "12 Lê Lợi, Q1",
        })
        self.assertEqual(created.status_code, 200, created.text)
        order_id = created.json()["id"]
        self.assertEqual(self.client.get(f"{base}/{order_id}").json()["customer_name"], "Chị Lan")
        status = self.client.post(f"{base}/{order_id}/status", json={"status": "packing"})
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "packing")
        self.assertEqual(len(self.client.get(base).json()), 1)
        self.assertEqual(self.client.delete(f"{base}/{order_id}").status_code, 200)
        self.assertEqual(self.client.get(f"{base}/{order_id}").status_code, 404)

    def test_picked_products_are_stored_and_read_back(self):
        self.catalogue.save_product("milo", {
            "name": "Milo", "category": "Đồ uống", "price": 15000, "stock": 9,
        })
        base = f"/api/accounts/{self.account_id}/orders"

        created = self.client.post(base, json={
            "customer_name": "Chị Lan", "total_amount": 30000, "shipping_address": "12 Lê Lợi",
            "line_items": [{"product_id": "milo", "quantity": 2}],
        })
        self.assertEqual(created.status_code, 200, created.text)
        order = self.client.get(f"{base}/{created.json()['id']}").json()
        self.assertEqual(order["items"], "2x Milo")
        self.assertEqual(order["line_items"], [
            {"product_id": "milo", "name": "Milo", "quantity": 2, "unit_price": 15000},
        ])

    def test_a_user_without_access_still_gets_nothing(self):
        stranger = self._client(self.stranger)
        for path in (
            f"/api/accounts/{self.account_id}/products",
            f"/api/accounts/{self.account_id}/orders",
        ):
            with self.subTest(path=path):
                # auth.check_account_access, via _account_or_404.
                self.assertEqual(stranger.get(path).status_code, 403)


if __name__ == "__main__":
    unittest.main()
