"""Guards on the combo/inventory fields the product form writes.

Availability for a combo is *derived* from its components rather than typed
in (see `ProductRepository._resolve_combo_stock`), which means a malformed
component list never fails loudly - it quietly resolves to "0 sellable", or
quotes a price for something the shop cannot pack. The edit that introduces
it is the only moment a person is there to fix it, so that is where these
rules live.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import dashboard_api.main as main
from dashboard_api.routes import products
from storage.product_repository import ProductRepository


class ComboValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)
        self.repo.save_product("sp1", {"name": "Sữa tắm", "stock": 10})
        self.repo.save_product("sp2", {"name": "Dầu gội", "stock": 10})

    def _validate(self, fields, product_id="combo1"):
        products._validate_combo(self.repo, product_id, fields)

    def test_a_valid_combo_passes(self):
        self._validate({
            "item_type": "combo",
            "combo_items": [
                {"product_id": "sp1", "quantity": 2},
                {"product_id": "sp2", "quantity": 1},
            ],
        })

    def test_a_single_item_is_not_subject_to_combo_rules(self):
        self._validate({"item_type": "single", "combo_items": []})

    def test_a_combo_needs_at_least_one_component(self):
        with self.assertRaises(HTTPException) as caught:
            self._validate({"item_type": "combo", "combo_items": []})
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_component_that_does_not_exist_is_rejected(self):
        with self.assertRaises(HTTPException):
            self._validate({
                "item_type": "combo",
                "combo_items": [{"product_id": "gone", "quantity": 1}],
            })

    def test_a_component_must_be_a_single_not_another_combo(self):
        """Nested combos would turn "how many can we still sell" into a graph
        walk with cycles to defend against, for no case the catalogue has."""
        self.repo.save_product("combo0", {
            "name": "Bộ khác", "item_type": "combo",
            "combo_items": [{"product_id": "sp1", "quantity": 1}],
        })
        with self.assertRaises(HTTPException):
            self._validate({
                "item_type": "combo",
                "combo_items": [{"product_id": "combo0", "quantity": 1}],
            })

    def test_a_combo_cannot_contain_itself(self):
        with self.assertRaises(HTTPException):
            self._validate({
                "item_type": "combo",
                "combo_items": [{"product_id": "combo1", "quantity": 1}],
            })

    def test_a_duplicated_component_is_rejected(self):
        """Two rows for one product make the quantity per combo ambiguous."""
        with self.assertRaises(HTTPException):
            self._validate({
                "item_type": "combo",
                "combo_items": [
                    {"product_id": "sp1", "quantity": 1},
                    {"product_id": "sp1", "quantity": 3},
                ],
            })

    def test_a_zero_quantity_is_rejected(self):
        with self.assertRaises(HTTPException):
            self._validate({
                "item_type": "combo",
                "combo_items": [{"product_id": "sp1", "quantity": 0}],
            })


class ItemTypeLockTests(unittest.TestCase):
    """Switching an item's type after it has been sold rewrites what its
    stock figure ever meant - a single's number is a count on a shelf, a
    combo's is derived from other products entirely - so history stops
    matching the goods. The form disables the control; this is the rule."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)
        # Complete products: required fields are checked on every edit.
        self.repo.save_product("sp1", {"name": "Sữa tắm", "category": "Tắm", "price": 90000, "stock": 10})
        self.repo.save_product("sp2", {"name": "Lẻ", "category": "Tắm", "price": 50000, "stock": 4})
        for target, value in (
            ("_products", lambda account: self.repo),
            ("_account_or_404", lambda account_id: SimpleNamespace(id=account_id)),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_changing_the_item_type_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            products.update_product("acct", "sp2", {"item_type": "combo"})
        self.assertEqual(caught.exception.status_code, 400)

    def test_an_edit_that_leaves_the_type_alone_is_fine(self):
        updated = products.update_product("acct", "sp2", {"name": "Lẻ đổi tên"})
        self.assertEqual(updated["name"], "Lẻ đổi tên")
        self.assertEqual(updated["item_type"], "single")

    def test_editing_a_combo_without_resending_its_type_keeps_combo_rules(self):
        """The form may send only the changed fields. The stored type still
        decides, so the component list cannot be blanked by omission."""
        self.repo.save_product("combo1", {
            "name": "Bộ", "item_type": "combo", "category": "Tắm", "price": 150000,
            "combo_items": [{"product_id": "sp1", "quantity": 2}],
        })
        with self.assertRaises(HTTPException):
            products.update_product("acct", "combo1", {"combo_items": []})

    def test_a_negative_stock_is_refused_and_nothing_is_written(self):
        with self.assertRaises(HTTPException) as caught:
            products.update_product("acct", "sp2", {"stock": -8})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Tồn kho", caught.exception.detail)
        self.assertEqual(self.repo.get_by_id("sp2")["stock"], 4)

    def test_a_new_product_with_negative_prices_is_refused(self):
        for field in ("price", "cost_price", "low_stock_threshold", "shelf_life_days"):
            with self.subTest(field=field):
                with self.assertRaises(HTTPException) as caught:
                    products.create_product("acct", f"new-{field}", {"name": "Áo", field: -1})
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIsNone(self.repo.get_by_id(f"new-{field}"))

    def test_a_non_number_is_refused(self):
        for value in ("-8", True, float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException):
                    products.update_product("acct", "sp2", {"stock": value})

    def test_zero_stock_and_cleared_optional_values_are_still_allowed(self):
        updated = products.update_product("acct", "sp2", {"stock": 0, "cost_price": None})
        self.assertEqual(updated["stock"], 0)

    def test_clearing_the_price_is_refused(self):
        """The agent quotes it to customers; see RequiredFieldTests."""
        with self.assertRaises(HTTPException) as caught:
            products.update_product("acct", "sp2", {"price": None})
        self.assertIn("đơn giá", caught.exception.detail)
        self.assertEqual(self.repo.get_by_id("sp2")["price"], 50000)



class RequiredFieldTests(unittest.TestCase):
    """A product needs a name, a category and a price above zero, and a
    single item needs its stock (0 is fine). Reported by testing: saving
    with only a name went through. The agent quotes `price` and answers
    "còn hàng không?" from `stock`, so a product without them is one it
    cannot sell. Every missing field is named at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)
        self.repo.save_product("sp1", {"name": "Sữa tắm", "category": "Tắm", "price": 90000, "stock": 10})
        for target, value in (
            ("_products", lambda account: self.repo),
            ("_account_or_404", lambda account_id: SimpleNamespace(id=account_id)),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def refused(self, call):
        with self.assertRaises(HTTPException) as caught:
            call()
        self.assertEqual(caught.exception.status_code, 400)
        return caught.exception.detail

    def test_the_reported_case_names_every_missing_field(self):
        detail = self.refused(lambda: products.create_product("acct", "moi", {"name": "Áo"}))
        for label in ("danh mục", "đơn giá", "tồn kho"):
            self.assertIn(label, detail)
        self.assertIsNone(self.repo.get_by_id("moi"))

    def test_a_complete_single_without_a_description_is_created(self):
        product = products.create_product("acct", "moi", {
            "name": "Áo", "category": "Thời trang", "price": 150000, "stock": 0,
        })
        self.assertEqual(product["stock"], 0)

    def test_blank_text_counts_as_missing(self):
        detail = self.refused(lambda: products.create_product("acct", "moi", {
            "name": "  ", "category": "\n", "price": 1, "stock": 1,
        }))
        self.assertIn("tên sản phẩm", detail)
        self.assertIn("danh mục", detail)

    def test_the_price_must_be_above_zero(self):
        base = {"name": "Áo", "category": "Thời trang", "stock": 1}
        self.assertEqual(
            self.refused(lambda: products.create_product("acct", "moi", {**base, "price": 0})),
            "Đơn giá phải lớn hơn 0",
        )

    def test_a_combo_is_not_asked_for_stock(self):
        product = products.create_product("acct", "bo", {
            "name": "Bộ", "item_type": "combo", "category": "Tắm", "price": 150000,
            "combo_items": [{"product_id": "sp1", "quantity": 2}],
        })
        self.assertEqual(product["item_type"], "combo")

    def test_an_older_incomplete_product_is_asked_for_what_it_lacks_on_edit(self):
        """save_product merges, so the check runs on the merged product: an
        edit of one field cannot leave the product incomplete."""
        self.repo.save_product("cu", {"name": "Hàng cũ", "stock": 3})
        detail = self.refused(lambda: products.update_product("acct", "cu", {"name": "Hàng cũ 2"}))
        self.assertIn("danh mục", detail)
        self.assertIn("đơn giá", detail)
        self.assertEqual(self.repo.get_by_id("cu")["name"], "Hàng cũ")


if __name__ == "__main__":
    unittest.main()
