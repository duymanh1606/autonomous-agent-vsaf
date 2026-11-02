"""Global search behind the dashboard's ⌘K palette.

Two layers:

* `dashboard_api.search` - the accent-insensitive matcher. Operators type
  "nguyen" and mean "Nguyễn"; every word must match; đ folds to d.
* `GET /api/accounts/{id}/search` - customers by identity *and* by message
  text (each conversation listed once, identity first), orders, products;
  capped per group; short terms return nothing rather than everything.

Credential-free. The route test seeds a real throwaway PostgreSQL schema
through the same storage code the agent uses, so the FK chain
(accounts -> conversations -> conversation_messages/customer_profile) is
the real one, not a hand-rolled fixture that could drift from the schema.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth
from dashboard_api.db_reader import AgentDatabaseReader
from dashboard_api.orders import OrderStore
from dashboard_api.search import fold, matches, matches_words, snippet
from storage.database import Database
from storage.product_repository import ProductRepository
from tests.support import SCHEMA_PATH, TemporaryPostgresSchema, requires_postgres


class FoldingTests(unittest.TestCase):
    def test_fold_strips_vietnamese_diacritics_and_d_bar(self):
        self.assertEqual(fold("Nguyễn Đức Ánh"), "nguyen duc anh")
        self.assertEqual(fold("ÁO THUN"), "ao thun")
        self.assertEqual(fold(None), "")

    def test_every_word_must_match_in_any_order(self):
        self.assertTrue(matches("nguyen 0912", "Nguyễn Văn A", "0912345678"))
        self.assertTrue(matches("0912 nguyen", "Nguyễn Văn A", "0912345678"))
        self.assertFalse(matches("nguyen 0999", "Nguyễn Văn A", "0912345678"))

    def test_matches_words_with_prefolded_tokens(self):
        words = ["nguyen", "0912"]
        self.assertTrue(matches_words(words, "Nguyễn Văn A", "0912345678"))
        self.assertFalse(matches_words(words, "Trần Văn B", "0912345678"))
        self.assertFalse(matches_words([], "anything"))

    def test_accented_query_matches_unaccented_data_too(self):
        self.assertTrue(matches("áo", "ao thun nam"))

    def test_blank_term_matches_nothing(self):
        self.assertFalse(matches("   ", "anything"))

    def test_snippet_centres_on_the_match(self):
        text = "x" * 100 + " giao hàng ở Đà Nẵng " + "y" * 100
        out = snippet(text, "da nang", radius=12)
        self.assertIn("Đà Nẵng", out)
        self.assertTrue(out.startswith("…") and out.endswith("…"))
        self.assertLess(len(out), 40)

    def test_snippet_without_a_match_falls_back_to_the_start(self):
        self.assertEqual(snippet("hello world", "zzz", radius=3), "hello")


@requires_postgres
class SearchRouteTests(unittest.TestCase):
    ACCOUNT = "acc_search"

    def setUp(self):
        main.clear_search_cache()
        self.addCleanup(main.clear_search_cache)
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.database = Database(self.schema.dsn, SCHEMA_PATH, account_id=self.ACCOUNT)
        self.addCleanup(self.database.close)
        self.database.register_account()

        self.reader = AgentDatabaseReader(self.schema.dsn, account_id=self.ACCOUNT)
        self.addCleanup(self.reader.close)
        self.orders = OrderStore(self.schema.dsn)
        self.addCleanup(self.orders.close)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.products = ProductRepository(self.tmp.name)

        account = mock.Mock(id=self.ACCOUNT)
        patches = [
            mock.patch.object(main, "_account_or_404", return_value=account),
            mock.patch.object(main, "_reader", return_value=self.reader),
            mock.patch.object(main, "_products", return_value=self.products),
            mock.patch.object(main, "orders_store", self.orders),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            # Session resolution for the middleware - any active user will do,
            # access is what `_account_or_404` (mocked above) would check.
            # `decode_session` (not `decode_session_token`) is what the auth
            # middleware and the /api/events handler read: it returns
            # `(user_id, issued_at)`, the second half being what a revocation
            # check compares against. issued_at=0 with a row that carries no
            # `sessions_valid_from` means "never revoked".
            mock.patch.object(main.auth, "decode_session", return_value=(1, 0)),
            mock.patch.object(main.users_store, "get_by_id",
                              return_value={"id": 1, "email": "t@x", "is_admin": True, "is_active": True}),
            mock.patch.object(main.users_store, "accessible_account_ids", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(main.app)
        self.client.cookies.set(auth.SESSION_COOKIE, "probe")

    # ---------------------------------------------------------------- seed
    def _conversation(self, conversation_id, customer_id, *, display_name=None,
                      username=None, phone=None, messages=()):
        conn = self.database.conn
        conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id, platform, customer_id) "
            "VALUES (%s, %s, 'tiktok_web', %s)",
            (self.ACCOUNT, conversation_id, customer_id),
        )
        if display_name or username or phone:
            conn.execute(
                "INSERT INTO customer_profile(account_id, conversation_id, username, display_name, phone) "
                "VALUES (%s, %s, %s, %s, %s)",
                (self.ACCOUNT, conversation_id, username, display_name, phone),
            )
        base = datetime.now(timezone.utc)
        for index, text in enumerate(messages):
            conn.execute(
                "INSERT INTO conversation_messages"
                "(account_id, message_id, conversation_id, customer_id, direction, text, created_at) "
                "VALUES (%s, %s, %s, %s, 'INBOUND', %s, %s)",
                (self.ACCOUNT, f"{conversation_id}-m{index}", conversation_id, customer_id,
                 text, base + timedelta(seconds=index)),
            )
        conn.commit()

    def _search(self, q, **params):
        return self.client.get(f"/api/accounts/{self.ACCOUNT}/search", params={"q": q, **params})

    # --------------------------------------------------------------- cases
    def test_short_terms_return_nothing(self):
        self._conversation("c1", "u1", display_name="Anh")
        body = self._search("a").json()
        self.assertEqual(body, {"customers": [], "orders": [], "products": []})

    def test_customer_by_name_is_accent_insensitive(self):
        self._conversation("c1", "u1", display_name="Nguyễn Đức Ánh", messages=["xin chào"])
        self._conversation("c2", "u2", display_name="Trần Bình", messages=["hi"])
        hits = self._search("nguyen duc").json()["customers"]
        self.assertEqual([h["conversation_id"] for h in hits], ["c1"])
        self.assertEqual(hits[0]["matched"], "identity")
        self.assertIsNone(hits[0]["snippet"])

    def test_customer_by_phone_and_handle(self):
        self._conversation("c1", "u1", username="shopper_x", phone="0912 345 678", messages=["ok"])
        self.assertEqual(self._search("0912").json()["customers"][0]["conversation_id"], "c1")
        self.assertEqual(self._search("shopper_x").json()["customers"][0]["conversation_id"], "c1")

    def test_customer_by_message_text_with_snippet_and_no_duplicate(self):
        self._conversation("c1", "u1", display_name="Khách A",
                           messages=["cho mình hỏi giao hàng ở Đà Nẵng mất mấy ngày"])
        # This one matches on *both* name and a message: listed once, as identity.
        self._conversation("c2", "u2", display_name="Đà Nẵng Store", messages=["ship Đà Nẵng nhé"])
        hits = self._search("da nang").json()["customers"]
        self.assertEqual({h["conversation_id"] for h in hits}, {"c1", "c2"})
        by_id = {h["conversation_id"]: h for h in hits}
        self.assertEqual(by_id["c2"]["matched"], "identity")
        self.assertEqual(by_id["c1"]["matched"], "message")
        self.assertIn("Đà Nẵng", by_id["c1"]["snippet"])
        self.assertEqual(by_id["c1"]["display_name"], "Khách A")

    def test_groups_are_capped_by_limit(self):
        for i in range(8):
            self._conversation(f"c{i}", f"u{i}", display_name=f"Khách {i}", messages=["."])
        self.assertEqual(len(self._search("khach", limit=3).json()["customers"]), 3)
        self.assertEqual(len(self._search("khach").json()["customers"]), 6)  # default

    def test_orders_by_id_name_items_and_address(self):
        a = self.orders.create(self.ACCOUNT, {"customer_name": "Lê Thị Hoa", "items": "Áo thun x2",
                                              "shipping_address": "Quận 7, TP.HCM"})
        self.orders.create(self.ACCOUNT, {"customer_name": "Phạm Văn Nam", "items": "Quần jean"})
        self.assertEqual([o["id"] for o in self._search("le thi").json()["orders"]], [a["id"]])
        self.assertEqual([o["id"] for o in self._search("ao thun").json()["orders"]], [a["id"]])
        self.assertEqual([o["id"] for o in self._search("quan 7").json()["orders"]], [a["id"]])
        self.assertEqual([o["id"] for o in self._search(f"#{a['id']}").json()["orders"]], [a["id"]])

    def test_products_by_name_brand_keywords_and_tags(self):
        self.products.save_product("sp001", {"name": "Sữa rửa mặt Cetaphil", "brand": "Cetaphil",
                                             "keywords": ["srm", "da nhạy cảm"], "tags": ["skincare"]})
        self.products.save_product("sp002", {"name": "Kem chống nắng", "brand": "La Roche-Posay"})
        ids = lambda q: [p["id"] for p in self._search(q).json()["products"]]
        self.assertEqual(ids("sua rua mat"), ["sp001"])
        self.assertEqual(ids("cetaphil"), ["sp001"])
        self.assertEqual(ids("da nhay cam"), ["sp001"])
        self.assertEqual(ids("skincare"), ["sp001"])
        self.assertEqual(ids("chong nang"), ["sp002"])
        self.assertEqual(ids("sp002"), ["sp002"])

    def test_one_query_returns_all_three_groups(self):
        self._conversation("c1", "u1", display_name="Hoa Mai", messages=["."])
        self.orders.create(self.ACCOUNT, {"customer_name": "Hoa Mai", "items": "x"})
        self.products.save_product("sp001", {"name": "Trà hoa mai"})
        body = self._search("hoa mai").json()
        self.assertEqual(len(body["customers"]), 1)
        self.assertEqual(len(body["orders"]), 1)
        self.assertEqual(len(body["products"]), 1)

    def test_search_candidate_cache_and_invalidation(self):
        self._conversation("c1", "u1", display_name="Hoa Mai", messages=["."])
        self.orders.create(self.ACCOUNT, {"customer_name": "Hoa Mai", "items": "x"})
        self.products.save_product("sp001", {"name": "Trà hoa mai"})

        # First search: cache miss, populates cache
        res1 = self._search("hoa mai").json()
        self.assertEqual(len(res1["orders"]), 1)

        # Directly insert into orders store behind the cache
        self.orders.create(self.ACCOUNT, {"customer_name": "Hoa Mai Vàng", "items": "y"})

        # Second search within TTL: cache hit, still returns 1 order from cached candidates
        res2 = self._search("hoa mai").json()
        self.assertEqual(len(res2["orders"]), 1)

        # Clear cache: next search should reflect the newly inserted order
        main.clear_search_cache(self.ACCOUNT)
        res3 = self._search("hoa mai").json()
        self.assertEqual(len(res3["orders"]), 2)

    def test_order_creation_invalidates_cache_via_api(self):
        self.orders.create(self.ACCOUNT, {"customer_name": "Lan", "items": "a"})
        self.assertEqual(len(self._search("lan").json()["orders"]), 1)

        # Create order through the API endpoint (which calls clear_search_cache)
        create_res = self.client.post(
            f"/api/accounts/{self.ACCOUNT}/orders",
            json={
                "customer_name": "Lan Hương", "items": "b",
                "total_amount": 100000, "shipping_address": "12 Lê Lợi",
            },
        )
        self.assertEqual(create_res.status_code, 200)

        # Cache was invalidated by the mutation route
        self.assertEqual(len(self._search("lan").json()["orders"]), 2)


if __name__ == "__main__":
    unittest.main()
