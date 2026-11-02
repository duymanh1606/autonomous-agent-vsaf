"""An order needs a customer, its items, a total above zero and an address.

Reported by testing: the form saved an order with only a name and a total,
leaving the items and the shipping address blank - an order nobody could
pack or ship. The server now refuses it (the form also says what is missing,
see dashboard/src/lib/orderForm.ts), naming every missing field at once so
the operator fixes the form in one pass. The note stays optional.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import dashboard_api.main as main
from dashboard_api.routes import orders


COMPLETE = {
    "customer_name": "Chị Lan", "items": "2x Milo", "total_amount": 560000,
    "shipping_address": "12 Lê Lợi, Q1", "note": "",
}


class ValidateOrderTests(unittest.TestCase):
    def refused(self, fields):
        with self.assertRaises(HTTPException) as caught:
            orders._validate_order(fields)
        self.assertEqual(caught.exception.status_code, 400)
        return caught.exception.detail

    def test_a_complete_order_without_a_note_is_accepted(self):
        orders._validate_order(COMPLETE)
        orders._validate_order({**COMPLETE, "note": None})

    def test_the_reported_case_names_both_missing_fields(self):
        detail = self.refused({"customer_name": "Hân", "total_amount": 560000})
        self.assertIn("sản phẩm / số lượng", detail)
        self.assertIn("địa chỉ giao hàng", detail)
        self.assertNotIn("tổng tiền", detail)

    def test_whitespace_counts_as_missing(self):
        detail = self.refused({**COMPLETE, "items": "   ", "shipping_address": "\n"})
        self.assertIn("sản phẩm / số lượng", detail)
        self.assertIn("địa chỉ giao hàng", detail)

    def test_the_total_is_required_and_above_zero(self):
        self.assertIn("tổng tiền", self.refused({**COMPLETE, "total_amount": None}))
        for bad in (0, -5):
            with self.subTest(total=bad):
                self.assertEqual(self.refused({**COMPLETE, "total_amount": bad}),
                                 "Tổng tiền phải lớn hơn 0")
        for bad in ("abc", True, float("nan")):
            with self.subTest(total=bad):
                self.assertEqual(self.refused({**COMPLETE, "total_amount": bad}),
                                 "Tổng tiền phải là một số")


class RouteTests(unittest.TestCase):
    """The rule sits in front of the store: a refused order is never written."""

    def setUp(self):
        self.store = mock.Mock()
        for target, value in (
            ("_account_or_404", lambda account_id: SimpleNamespace(id=account_id)),
            ("orders_store", self.store),
            ("clear_search_cache", lambda account_id=None: None),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_refuses_before_writing(self):
        with self.assertRaises(HTTPException):
            orders.create_order("acct", {"customer_name": "Hân", "total_amount": 560000})
        self.store.create.assert_not_called()

    def test_a_full_edit_is_held_to_the_same_rule(self):
        with self.assertRaises(HTTPException):
            orders.update_order("acct", 1, {**COMPLETE, "shipping_address": ""})
        self.store.update.assert_not_called()

    def test_a_status_change_on_an_older_incomplete_order_still_works(self):
        """Orders saved before this rule may lack an address; moving one to
        packing or shipping is not an edit of those fields."""
        self.store.set_status.return_value = {"id": 1, "status": "packing"}
        with mock.patch.object(orders, "_export_order"):
            result = orders.set_order_status(
                "acct", 1, {"status": "packing"}, background_tasks=mock.Mock(),
            )
        self.assertEqual(result["status"], "packing")



class LineItemTests(unittest.TestCase):
    """Products picked from the catalogue. Names and prices come from the
    shop's own catalogue, never from the request; a line already on an order
    keeps the price it was taken at."""

    def setUp(self):
        catalogue = {
            "milo": {"id": "milo", "name": "Milo", "price": 15000, "stock": 10},
            "sua": {"id": "sua", "name": "Sữa Ông Thọ", "price": 25000, "stock": 5},
        }
        self.catalogue = catalogue
        repo = SimpleNamespace(get_by_id=lambda product_id: catalogue.get(product_id))
        patcher = mock.patch.object(main, "_products", lambda account: repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = SimpleNamespace(id="acct")

    def resolve(self, lines, stored=None):
        return orders._line_items(self.account, {**COMPLETE, "items": "", "line_items": lines}, stored)

    def test_names_and_prices_come_from_the_catalogue(self):
        fields = self.resolve([
            {"product_id": "milo", "quantity": 2, "name": "Bịa", "unit_price": 1},
            {"product_id": "sua", "quantity": 1},
        ])
        self.assertEqual(fields["items"], "2x Milo, 1x Sữa Ông Thọ")
        self.assertEqual(fields["line_items"], [
            {"product_id": "milo", "name": "Milo", "quantity": 2, "unit_price": 15000},
            {"product_id": "sua", "name": "Sữa Ông Thọ", "quantity": 1, "unit_price": 25000},
        ])

    def test_the_same_product_twice_is_one_line(self):
        fields = self.resolve([{"product_id": "milo", "quantity": 2}, {"product_id": "milo", "quantity": 3}])
        self.assertEqual(fields["items"], "5x Milo")

    def test_an_unknown_product_or_a_bad_quantity_is_refused(self):
        for lines in (
            [{"product_id": "khong-co", "quantity": 1}],
            [{"product_id": "milo", "quantity": 0}],
            [{"product_id": "milo", "quantity": 1.5}],
            [{"product_id": "milo", "quantity": True}],
            ["milo"],
        ):
            with self.subTest(lines=lines):
                with self.assertRaises(HTTPException) as caught:
                    self.resolve(lines)
                self.assertEqual(caught.exception.status_code, 400)

    def test_an_edit_keeps_the_price_and_name_the_order_was_taken_at(self):
        stored = [{"product_id": "milo", "name": "Milo hộp", "quantity": 1, "unit_price": 12000}]
        self.catalogue["milo"]["price"] = 99000
        del self.catalogue["sua"]  # also fine for a line already on the order
        stored.append({"product_id": "sua", "name": "Sữa Ông Thọ", "quantity": 1, "unit_price": 25000})
        fields = self.resolve(
            [{"product_id": "milo", "quantity": 3}, {"product_id": "sua", "quantity": 1}], stored,
        )
        self.assertEqual(fields["line_items"][0]["unit_price"], 12000)
        self.assertEqual(fields["items"], "3x Milo hộp, 1x Sữa Ông Thọ")

    def test_more_than_the_stock_is_refused_naming_every_short_line(self):
        with self.assertRaises(HTTPException) as caught:
            self.resolve([{"product_id": "milo", "quantity": 11}, {"product_id": "sua", "quantity": 6}])
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Milo (còn 10, đặt 11)", caught.exception.detail)
        self.assertIn("Sữa Ông Thọ (còn 5, đặt 6)", caught.exception.detail)
        # Exactly the stock is fine; picking the same product twice counts once.
        self.resolve([{"product_id": "milo", "quantity": 6}, {"product_id": "milo", "quantity": 4}])
        with self.assertRaises(HTTPException):
            self.resolve([{"product_id": "milo", "quantity": 6}, {"product_id": "milo", "quantity": 5}])

    def test_an_edit_that_does_not_raise_a_line_is_not_held_up_by_stock(self):
        """Stock that fell after the order was taken must not stop the
        operator correcting the address."""
        stored = [{"product_id": "sua", "name": "Sữa Ông Thọ", "quantity": 8, "unit_price": 25000}]
        fields = self.resolve([{"product_id": "sua", "quantity": 8}], stored)
        self.assertEqual(fields["items"], "8x Sữa Ông Thọ")
        with self.assertRaises(HTTPException):
            self.resolve([{"product_id": "sua", "quantity": 9}], stored)

    def test_a_product_with_no_stock_recorded_counts_as_none_left(self):
        self.catalogue["moi"] = {"id": "moi", "name": "Hàng mới", "price": 1000, "stock": None}
        with self.assertRaises(HTTPException) as caught:
            self.resolve([{"product_id": "moi", "quantity": 1}])
        self.assertIn("còn 0", caught.exception.detail)

    def test_an_order_without_picked_products_keeps_its_typed_items(self):
        """Orders logged before the picker existed."""
        fields = orders._line_items(self.account, {**COMPLETE, "items": "2x Milo (ghi tay)"})
        self.assertEqual(fields["items"], "2x Milo (ghi tay)")
        self.assertEqual(fields["line_items"], [])


if __name__ == "__main__":
    unittest.main()
