"""A product question that names no product is about the shop's product.

Most shops sell one product, so "giá bao nhiêu?" can only mean that one.
The agent used to answer it by asking "bạn hỏi sản phẩm nào?". Now, in order:
the product the shop set as its default (`products.default_product_id`), if
it is still a single item on sale; else the only single item on sale; else
(several singles, no default) the agent still asks.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.product_lookup import ProductLookupPolicy
from control.settings import apply_runtime_settings
from load_config import load_config
from models.analysis import MessageAnalysis
from storage.product_repository import ProductRepository

CALCIUM = {"name": "Calcium Gold", "price": 124000, "stock": 50}
COMBO = {"name": "Combo 2", "price": 220000, "item_type": "combo",
         "combo_items": [{"product_id": "cg", "quantity": 2}]}


def catalogue(test, products):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for product_id, product in products.items():
        (root / product_id).mkdir()
        (root / product_id / "product.json").write_text(
            json.dumps({"id": product_id, **product}), encoding="utf-8")
    return ProductRepository(root)


def price_question(**overrides):
    data = {"intent": "ask_price", "next_stage": "asking_price", "requires_tool": False,
            "tool_name": None, "tool_args": {}, "confidence": 0.9}
    data.update(overrides)
    return MessageAnalysis(**data)


class DefaultProductResolutionTests(unittest.TestCase):
    def test_the_only_single_item_is_the_default_and_combos_do_not_count(self):
        repo = catalogue(self, {"cg": CALCIUM, "cg-2": COMBO})
        self.assertEqual(repo.default_product_id(), "cg")

    def test_several_singles_and_no_choice_means_no_default(self):
        repo = catalogue(self, {"cg": CALCIUM, "milk": {"name": "Milk", "price": 1}})
        self.assertIsNone(repo.default_product_id())

    def test_the_shops_choice_wins_over_several_singles(self):
        repo = catalogue(self, {"cg": CALCIUM, "milk": {"name": "Milk", "price": 1}})
        self.assertEqual(repo.default_product_id("milk"), "milk")

    def test_a_stopped_deleted_or_combo_choice_is_not_used(self):
        repo = catalogue(self, {
            "cg": CALCIUM, "cg-2": COMBO,
            "old": {"name": "Old", "price": 1, "is_active": False},
        })
        for choice in ("old", "gone", "cg-2"):
            with self.subTest(choice=choice):
                # Falls back to the only single item still on sale.
                self.assertEqual(repo.default_product_id(choice), "cg")

    def test_a_stopped_only_product_is_never_the_default(self):
        repo = catalogue(self, {"old": {"name": "Old", "price": 1, "is_active": False}})
        self.assertIsNone(repo.default_product_id())


class PriceQuestionTests(unittest.TestCase):
    def test_a_price_question_naming_nothing_looks_up_the_shops_product(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(self, {"cg": CALCIUM, "cg-2": COMBO}))
        analysis = policy.apply(price_question())
        self.assertTrue(analysis.requires_tool)
        self.assertEqual(analysis.tool_name, "get_product")
        self.assertEqual(analysis.tool_args, {"product_id": "cg"})

    def test_media_and_stock_questions_too(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(self, {"cg": CALCIUM}))
        media = policy.apply(price_question(intent="ask_product", requested_media=["image"]))
        self.assertEqual((media.tool_name, media.tool_args), ("get_product", {"product_id": "cg"}))
        stock = policy.apply(price_question(intent="check_inventory"))
        self.assertEqual((stock.tool_name, stock.tool_args), ("check_inventory", {"product_id": "cg"}))

    def test_an_analyzer_tool_call_without_arguments_gets_the_default(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(self, {"cg": CALCIUM}))
        analysis = policy.apply(price_question(requires_tool=True, tool_name="get_product"))
        self.assertEqual(analysis.tool_args, {"product_id": "cg"})

    def test_a_named_product_still_wins(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(self, {"cg": CALCIUM}))
        analysis = policy.apply(price_question(entities={"product_name": "Milo"}))
        self.assertEqual(analysis.tool_args, {"product_id": "Milo"})

    def test_without_a_default_the_agent_still_asks(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(
            self, {"cg": CALCIUM, "milk": {"name": "Milk", "price": 1}}))
        analysis = policy.apply(price_question())
        self.assertFalse(analysis.requires_tool)
        self.assertIsNone(analysis.tool_name)

    def test_the_configured_default_is_used(self):
        policy = ProductLookupPolicy(True, default_product_id="milk", catalogue=catalogue(
            self, {"cg": CALCIUM, "milk": {"name": "Milk", "price": 1}}))
        self.assertEqual(policy.apply(price_question()).tool_args, {"product_id": "milk"})

    def test_a_broken_catalogue_means_asking_not_guessing(self):
        broken = SimpleNamespace(default_product_id=mock.Mock(side_effect=OSError("disk")))
        analysis = ProductLookupPolicy(True, catalogue=broken).apply(price_question())
        self.assertFalse(analysis.requires_tool)

    def test_lookup_disabled_is_unchanged(self):
        policy = ProductLookupPolicy(False, catalogue=catalogue(self, {"cg": CALCIUM}))
        self.assertFalse(policy.apply(price_question()).requires_tool)

    def test_the_choice_is_applied_live(self):
        policy = ProductLookupPolicy(True, catalogue=catalogue(
            self, {"cg": CALCIUM, "milk": {"name": "Milk", "price": 1}}))
        agent = SimpleNamespace(inbox_skill=SimpleNamespace(
            reply_provider=SimpleNamespace(product_lookup=policy)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text("products: {default_product_id: null}\n", encoding="utf-8")
        result = apply_runtime_settings(agent, load_config(path), {"products.default_product_id": "milk"})
        self.assertIn("products.default_product_id", result["applied"])
        self.assertEqual(policy.apply(price_question()).tool_args, {"product_id": "milk"})


class DefaultProductRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from dashboard_api import main
        from dashboard_api.routes import products

        self.routes = products
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"
        shutil.copy(Path(__file__).resolve().parents[1] / "configs" / "config.yaml", self.path)
        repo = catalogue(self, {
            "cg": CALCIUM, "cg-2": COMBO, "milk": {"name": "Milk", "price": 1},
            "old": {"name": "Old", "price": 1, "is_active": False},
        })
        account = SimpleNamespace(id="acc", control_port=28999)
        self.control = mock.AsyncMock(return_value={})
        for target, value in (
            ("registry", mock.Mock(config=lambda account: load_config(self.path))),
            ("_account_or_404", lambda account_id: account),
            ("_products", lambda account: repo),
            ("_control", self.control),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_single_item_is_saved_pushed_live_and_shown(self):
        body = await self.routes.write_default_product("acc", product_id="milk")
        self.assertEqual(body, {"default_product_id": "milk", "effective_default_product_id": "milk"})
        self.assertEqual(load_config(self.path).get("products.default_product_id"), "milk")
        self.control.assert_awaited_once()
        listed = self.routes.read_products("acc")
        self.assertEqual(listed["effective_default_product_id"], "milk")

    async def test_clearing_goes_back_to_automatic(self):
        await self.routes.write_default_product("acc", product_id="milk")
        body = await self.routes.write_default_product("acc", product_id=None)
        # Two singles on sale: automatic finds none, so the agent asks.
        self.assertEqual(body, {"default_product_id": None, "effective_default_product_id": None})

    async def test_a_combo_a_stopped_or_an_unknown_product_is_refused(self):
        from fastapi import HTTPException

        for product_id, status in (("cg-2", 422), ("old", 422), ("gone", 404)):
            with self.subTest(product_id=product_id), self.assertRaises(HTTPException) as raised:
                await self.routes.write_default_product("acc", product_id=product_id)
            self.assertEqual(raised.exception.status_code, status)
        self.assertIsNone(load_config(self.path).get("products.default_product_id"))
        self.control.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
