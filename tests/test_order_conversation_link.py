"""An order keeps its conversation across edits.

The sheet export finds an order's row by its conversation, and the order form
once never sent `conversation_id`: every edit unlinked the order, which then
never reached the Google Sheet again.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support import requires_postgres
from tests.test_dashboard_routes_split import DashboardRouteFixture, order_routes

FIELDS = {
    "customer_name": "Chị Lan", "items": "2x Milo",
    "total_amount": 30000, "shipping_address": "12 Lê Lợi, Q1",
}


@requires_postgres
class OrderConversationLinkTests(DashboardRouteFixture):
    def setUp(self):
        super().setUp()
        self.base = f"/api/accounts/{self.account_id}/orders"
        created = self.client.post(self.base, json={**FIELDS, "conversation_id": "0:1:111:999"})
        self.assertEqual(created.status_code, 200, created.text)
        self.order = created.json()

    def edit(self, **fields):
        response = self.client.put(f"{self.base}/{self.order['id']}", json=fields)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_edit_without_the_field_keeps_the_link(self):
        edited = self.edit(**{**FIELDS, "shipping_address": "99 Trần Phú"})
        self.assertEqual(edited["conversation_id"], "0:1:111:999")
        # ... and so the edit still reaches the sheet, with the new address.
        sent = order_routes.export_order.call_args.args[0]
        self.assertEqual(order_routes.export_order.call_args.kwargs["shop"], "shop")  # the label
        self.assertEqual(
            (sent["conversation_id"], sent["shipping_address"]), ("0:1:111:999", "99 Trần Phú"),
        )

    def test_an_explicit_empty_link_unlinks(self):
        self.assertIsNone(self.edit(**FIELDS, conversation_id=None)["conversation_id"])


if __name__ == "__main__":
    unittest.main()
