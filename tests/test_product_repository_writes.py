"""Create/update/delete product + media upload/removal, added so products can
be managed from the dashboard instead of only edited by hand on disk."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.product_repository import ProductRepository


class ProductRepositoryWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)

    def test_save_product_creates_a_new_product_directory(self):
        product = self.repo.save_product("sp100", {"name": "Milo mới", "price": 20000})
        self.assertEqual(product["id"], "sp100")
        self.assertEqual(product["name"], "Milo mới")
        self.assertEqual(product["price"], 20000)
        self.assertTrue((Path(self.tmp.name) / "sp100" / "product.json").is_file())

    def test_save_product_update_does_not_drop_existing_media(self):
        self.repo.save_product("sp101", {"name": "A"})
        self.repo.save_media("sp101", "image", "front.jpg", b"fake-bytes")
        updated = self.repo.save_product("sp101", {"name": "B"})
        self.assertEqual(updated["name"], "B")
        self.assertEqual(len(updated["media"]["images"]), 1)

    def test_save_product_rejects_a_path_traversal_id(self):
        with self.assertRaises(ValueError):
            self.repo.save_product("../escape", {"name": "bad"})

    def test_delete_product_removes_the_directory(self):
        self.repo.save_product("sp102", {"name": "C"})
        self.assertTrue(self.repo.delete_product("sp102"))
        self.assertIsNone(self.repo.get_by_id("sp102"))

    def test_delete_product_unknown_id_returns_false(self):
        self.assertFalse(self.repo.delete_product("does-not-exist"))

    def test_save_media_registers_the_file_in_product_json(self):
        self.repo.save_product("sp103", {"name": "D"})
        relative = self.repo.save_media("sp103", "image", "front.jpg", b"data")
        self.assertTrue(relative.startswith("images/"))
        product = self.repo.get_by_id("sp103")
        images = product["media"]["images"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["exists"])
        self.assertTrue(images[0]["valid_type"])

    def test_save_media_rejects_unsupported_extension(self):
        self.repo.save_product("sp104", {"name": "E"})
        with self.assertRaises(ValueError):
            self.repo.save_media("sp104", "image", "malware.exe", b"data")

    def test_save_media_rejects_unknown_product(self):
        with self.assertRaises(ValueError):
            self.repo.save_media("no-such-product", "image", "a.jpg", b"data")

    def test_delete_media_removes_file_and_json_reference(self):
        self.repo.save_product("sp105", {"name": "F"})
        relative = self.repo.save_media("sp105", "video", "clip.mp4", b"video-bytes")
        self.assertTrue(self.repo.delete_media("sp105", relative))
        product = self.repo.get_by_id("sp105")
        self.assertEqual(product["media"]["videos"], [])
        self.assertFalse((Path(self.tmp.name) / "sp105" / relative).exists())

    def test_delete_media_path_traversal_is_refused(self):
        self.repo.save_product("sp106", {"name": "G"})
        outside = str(Path(self.tmp.name).parent / "secret.txt")
        self.assertFalse(self.repo.delete_media("sp106", f"../../{outside}"))

    def test_two_accounts_have_independent_catalogues(self):
        with tempfile.TemporaryDirectory() as other_root:
            other_repo = ProductRepository(product_root=other_root)
            self.repo.save_product("sp200", {"name": "Only in account A"})
            self.assertIsNone(other_repo.get_by_id("sp200"))
            self.assertIsNotNone(self.repo.get_by_id("sp200"))


class ComboStockTests(unittest.TestCase):
    """A combo is sold as one line but held in stock as its parts, so its
    availability is the number of combos still assemblable - never a figure
    anyone typed in. Every existing consumer asks `stock`, so that is the key
    the answer has to arrive under."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)
        self.repo.save_product("sp1", {"name": "Sữa tắm", "stock": 100})
        self.repo.save_product("sp2", {"name": "Dầu gội", "stock": 7})

    def _combo(self, items, **extra):
        return self.repo.save_product("combo1", {
            "name": "Bộ đôi", "item_type": "combo", "combo_items": items, **extra,
        })

    def test_stock_is_the_scarcest_component(self):
        combo = self._combo([
            {"product_id": "sp1", "quantity": 4},   # 100 / 4  = 25
            {"product_id": "sp2", "quantity": 2},   #   7 / 2  =  3
        ])
        self.assertEqual(combo["stock"], 3)

    def test_stock_follows_a_component_being_sold_down(self):
        """The combo's own file never changes when a component's stock does,
        so a cached figure would keep promising combos that can no longer be
        packed."""
        self._combo([{"product_id": "sp2", "quantity": 1}])
        self.assertEqual(self.repo.get_by_id("combo1")["stock"], 7)

        self.repo.save_product("sp2", {"stock": 1})

        self.assertEqual(self.repo.get_by_id("combo1")["stock"], 1)

    def test_a_missing_component_makes_the_combo_unsellable(self):
        self._combo([
            {"product_id": "sp1", "quantity": 1},
            {"product_id": "gone", "quantity": 1},
        ])
        self.assertEqual(self.repo.get_by_id("combo1")["stock"], 0)

    def test_a_combo_with_no_components_is_not_unlimited(self):
        self.assertEqual(self._combo([])["stock"], 0)

    def test_deriving_stock_never_corrupts_the_cached_product(self):
        self._combo([{"product_id": "sp2", "quantity": 2}])
        self.repo.get_by_id("combo1")
        self.assertEqual(self.repo.get_by_id("combo1")["stock"], 3)

    def test_a_single_product_keeps_its_own_typed_in_stock(self):
        self.assertEqual(self.repo.get_by_id("sp1")["stock"], 100)


class CatalogueDefaultsTests(unittest.TestCase):
    """A catalogue written before combos existed has none of these keys, and
    must keep behaving exactly as it did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = ProductRepository(product_root=self.tmp.name)

    def test_an_older_product_reads_as_an_active_single(self):
        product = self.repo.save_product("sp1", {"name": "Cũ", "stock": 5})
        self.assertEqual(product["item_type"], "single")
        self.assertTrue(product["is_active"])
        self.assertEqual(product["stock"], 5)
        self.assertEqual(product["combo_items"], [])

    def test_search_hides_a_withdrawn_product(self):
        """`search` is what the reply pipeline offers customers from, so a
        product switched off must stop being recommended - while staying
        readable by id for conversations already about it."""
        self.repo.save_product("sp1", {"name": "Sữa tắm", "is_active": True})
        self.repo.save_product("sp2", {"name": "Sữa tắm cũ", "is_active": False})

        found = {item["id"] for item in self.repo.search("sữa tắm", limit=10)}

        self.assertEqual(found, {"sp1"})
        self.assertIsNotNone(self.repo.get_by_id("sp2"))

    def test_a_component_row_without_a_quantity_is_dropped(self):
        """Kept as a zero it would divide into "unlimited" and promise a
        combo that cannot be assembled."""
        product = self.repo.save_product("combo1", {
            "name": "Bộ", "item_type": "combo", "combo_items": [
                {"product_id": "sp1", "quantity": 0},
                {"product_id": "", "quantity": 3},
                {"product_id": "sp2", "quantity": 2},
            ],
        })
        self.assertEqual(
            product["combo_items"], [{"product_id": "sp2", "quantity": 2.0}],
        )


if __name__ == "__main__":
    unittest.main()
