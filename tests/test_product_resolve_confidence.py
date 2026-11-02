"""`ProductRepository.resolve` - the reply pipeline's product lookup.

`search` ranks anything sharing one word with the query, which suits a
browsing list and not a customer-facing reply: "sữa chua 100g" against Milo,
Ông Thọ and sữa tắm ranked Milo first, and the agent sent a Milo photo for a
yogurt request. `resolve` is what `get_product`/`check_inventory` fall back to
now: most of the customer's words must hit, and no other product may tie.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.product_repository import ProductRepository


class ProductResolveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)
        self.repo.save_product("sp001", {
            "name": "Sữa Milo 180ml", "brand": "Nestlé", "category": "Sữa",
            "keywords": ["milo", "sữa milo"], "price": 9000, "stock": 10,
            "description": "Nước ngọt lúa mạch",
        })
        self.repo.save_product("sp003", {
            "name": "Sữa Ông Thọ 380g", "category": "Sữa",
            "keywords": ["ông thọ", "sữa đặc"], "price": 30000, "stock": 3,
        })
        self.repo.save_product("sp006", {
            "name": "Sữa tắm Venus BigOne", "keywords": ["venus", "sữa tắm"],
            "price": 120000, "stock": 5,
        })

    def test_a_product_the_shop_does_not_sell_resolves_to_nothing(self):
        for query in ("sữa chua 100g", "sữa chua", "bánh mì", "sữa chua vinamilk"):
            with self.subTest(query=query):
                self.assertIsNone(self.repo.resolve(query))

    def test_one_shared_word_is_ambiguity_not_a_match(self):
        self.assertIsNone(self.repo.resolve("sữa"))

    def test_the_named_product_still_resolves(self):
        for query, expected in (
            ("milo", "sp001"), ("sữa milo 180ml", "sp001"), ("Sữa Milo 180ml", "sp001"),
            ("sữa milo hộp", "sp001"), ("ông thọ", "sp003"),
            ("sữa đặc ông thọ 380g", "sp003"), ("sữa tắm venus", "sp006"), ("sp003", "sp003"),
        ):
            with self.subTest(query=query):
                self.assertEqual(self.repo.resolve(query)["id"], expected)

    def test_search_keeps_ranking_loosely_for_browsing(self):
        self.assertEqual(
            [p["id"] for p in self.repo.search("sữa", limit=10)],
            ["sp001", "sp003", "sp006"],
        )

    async def test_get_product_and_check_inventory_return_empty_instead_of_guessing(self):
        self.assertEqual(await self.repo.get_product("sữa chua 100g"), {})
        self.assertEqual((await self.repo.get_product("milo"))["id"], "sp001")
        self.assertEqual(await self.repo.check_inventory("sữa chua 100g"), {})


if __name__ == "__main__":
    unittest.main()
