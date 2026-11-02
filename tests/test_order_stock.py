"""Orders take their products out of stock, and give them back.

Reported by testing: an order for 150 units was created and the product's
stock still read 150. Now an order holds its products from the moment it is
placed (pending, packing, shipping, done); cancelling or returning it puts
them back; deleting it puts them back only while the goods cannot have left
(pending, packing). An edit moves only the difference. A combo takes from
its components. Orders from before this change never move stock.

All through the real HTTP routes, a real order store and a real catalogue.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.product_repository import ProductRepository
from tests.support import requires_postgres
from tests.test_dashboard_routes_split import DashboardRouteFixture


@requires_postgres
class OrderStockTests(DashboardRouteFixture):
    def setUp(self):
        super().setUp()
        self.catalogue.save_product("ao", {"name": "Áo babytee", "category": "Áo", "price": 260000, "stock": 150})
        self.catalogue.save_product("quan", {"name": "Quần", "category": "Quần", "price": 300000, "stock": 10})
        self.catalogue.save_product("bo", {
            "name": "Bộ áo quần", "item_type": "combo", "category": "Bộ", "price": 500000,
            "combo_items": [{"product_id": "ao", "quantity": 1}, {"product_id": "quan", "quantity": 1}],
        })
        self.base = f"/api/accounts/{self.account_id}/orders"

    def stock(self, product_id):
        return self.catalogue.get_by_id(product_id)["stock"]

    def order(self, lines, **extra):
        response = self.client.post(self.base, json={
            "customer_name": "ThanhThuyNe", "total_amount": 1000, "shipping_address": "Hà Nội",
            "line_items": [{"product_id": p, "quantity": q} for p, q in lines], **extra,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def edit(self, order, lines, **extra):
        return self.client.put(f"{self.base}/{order['id']}", json={
            "customer_name": order["customer_name"], "total_amount": 1000,
            "shipping_address": "Hà Nội", "status": order["status"],
            "line_items": [{"product_id": p, "quantity": q} for p, q in lines], **extra,
        })

    def status(self, order, value):
        response = self.client.post(f"{self.base}/{order['id']}/status", json={"status": value})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_reported_case_an_order_of_150_takes_150(self):
        order = self.order([("ao", 150)])
        self.assertTrue(order["stock_deducted"])
        self.assertEqual(self.stock("ao"), 0)

    def test_more_than_the_stock_is_refused_and_nothing_moves(self):
        response = self.client.post(self.base, json={
            "customer_name": "A", "total_amount": 1, "shipping_address": "B",
            "line_items": [{"product_id": "ao", "quantity": 151}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("vượt tồn kho", response.json()["detail"])
        self.assertEqual(self.stock("ao"), 150)
        self.assertEqual(self.client.get(self.base).json(), [])

    def test_a_combo_takes_from_its_components(self):
        self.order([("bo", 3)])
        self.assertEqual(self.stock("ao"), 147)
        self.assertEqual(self.stock("quan"), 7)
        self.assertEqual(self.stock("bo"), 7)  # derived: min of the parts

    def test_an_edit_moves_only_the_difference(self):
        order = self.order([("ao", 100)])
        self.assertEqual(self.stock("ao"), 50)
        # Up to everything there is: 100 held + 50 left.
        self.assertEqual(self.edit(order, [("ao", 150)]).status_code, 200)
        self.assertEqual(self.stock("ao"), 0)
        # Correcting the address with the line unchanged needs no stock.
        self.assertEqual(self.edit(order, [("ao", 150)]).status_code, 200)
        # Down, and swapping a product.
        self.assertEqual(self.edit(order, [("ao", 20), ("quan", 4)]).status_code, 200)
        self.assertEqual((self.stock("ao"), self.stock("quan")), (130, 6))
        # More than there is, even counting what it holds: refused, nothing moves.
        refused = self.edit(order, [("ao", 151), ("quan", 4)])
        self.assertEqual(refused.status_code, 400)
        self.assertEqual((self.stock("ao"), self.stock("quan")), (130, 6))

    def test_cancelling_or_returning_gives_the_stock_back(self):
        cancelled = self.order([("ao", 40)])
        self.status(cancelled, "cancelled")
        self.assertEqual(self.stock("ao"), 150)

        shipped = self.status(self.status(self.order([("ao", 30)]), "packing"), "shipping")
        self.assertEqual(self.stock("ao"), 120)
        self.status(shipped, "returned")
        self.assertEqual(self.stock("ao"), 150)

    def test_deleting_gives_stock_back_only_before_it_ships(self):
        pending = self.order([("ao", 10)])
        self.assertEqual(self.client.delete(f"{self.base}/{pending['id']}").status_code, 200)
        self.assertEqual(self.stock("ao"), 150)

        shipped = self.order([("ao", 10)])
        self.status(self.status(shipped, "packing"), "shipping")
        self.client.delete(f"{self.base}/{shipped['id']}")
        self.assertEqual(self.stock("ao"), 140)  # the goods have left

    def test_an_order_from_before_this_change_never_moves_stock(self):
        legacy = self.orders.create(self.account_id, {
            "customer_name": "Cũ", "items": "5x Áo babytee", "total_amount": 1, "shipping_address": "x",
            "line_items": [{"product_id": "ao", "name": "Áo babytee", "quantity": 5, "unit_price": 1}],
        })
        self.assertFalse(legacy["stock_deducted"])
        self.status(legacy, "cancelled")
        self.assertEqual(self.stock("ao"), 150)


class AdjustStockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = ProductRepository(product_root=tmp.name)
        self.repo.save_product("a", {"name": "A", "stock": 5})
        self.repo.save_product("b", {"name": "B", "stock": 1})

    def test_all_or_nothing(self):
        with self.assertRaises(ValueError) as caught:
            self.repo.adjust_stock({"a": -2, "b": -3})
        self.assertIn("B (còn 1, cần 3)", str(caught.exception))
        self.assertEqual((self.repo.get_by_id("a")["stock"], self.repo.get_by_id("b")["stock"]), (5, 1))

    def test_returning_stock_to_a_deleted_product_is_skipped(self):
        self.repo.delete_product("b")
        self.assertEqual(self.repo.adjust_stock({"a": 1, "b": 2}), {"a": 6})

    def test_writes_leave_no_temporary_files(self):
        self.repo.adjust_stock({"a": -1})
        leftovers = [p.name for p in Path(self.repo.product_root, "a").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
