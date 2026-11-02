"""What a sales playbook (the Calcium Gold one, for instance) needs from the
system before its rules can work:

1. shop-wide delivery facts the agent may quote (products.shipping);
2. totals = quantity x verified price (+ verified shipping fee) accepted;
3. "kyat(s)" recognised as a currency, so those amounts are checked at all;
4. order-status questions handed to a person (nobody can look a parcel up);
5. taste/colour/preparation comments kept with the agent (product_feedback);
6. an ordering customer with phone + address handed to a person to create
   the order - once - with a notification that says so.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRoute, MessageRouter
from agent.reply_service import ORDER_READY_REASON, ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.shop_policy import ShopPolicy, clean_shipping_setting, normalize_shipping
from agent.state_manager import CustomerStateManager
from control.settings import apply_runtime_settings
from dashboard_api.notifications import NotificationStore
from load_config import load_config
from models.reply import ReplyResult
from models.tool import ToolResult
from storage.product_repository import ProductRepository
from tests.test_reply_pipeline_parity import STAGES, StubAnalyzer, StubStore, analysis

SHIPPING = {"fee": 6000, "free_from_quantity": 2, "delivery_days": "2-5",
            "cash_on_delivery": True, "area": "Toàn Myanmar"}


def priced(text, data):
    return ReplyValidator._has_unverified_price_claim(
        text, ToolResult(tool_name="get_product", success=True, data=data),
    )


class ShippingFactsTests(unittest.TestCase):
    def test_the_facts_are_named_for_the_model_and_the_validator(self):
        self.assertEqual(normalize_shipping(SHIPPING), {
            "shipping_fee": 6000, "free_shipping_from_quantity": 2,
            "delivery_days": "2-5", "cash_on_delivery": True,
            "delivery_area": "Toàn Myanmar",
        })
        self.assertEqual(normalize_shipping(None), {})
        self.assertEqual(normalize_shipping({"fee": -1, "free_from_quantity": 0}), {})

    def test_the_dashboard_form_is_validated(self):
        self.assertEqual(clean_shipping_setting({"fee": 6000, "area": " "}), {"fee": 6000})
        self.assertIsNone(clean_shipping_setting({"fee": "", "area": ""}))
        for bad in ({"fee": -5}, {"fee": "6000"}, {"free_from_quantity": 1.5},
                    {"delivery_days": "x" * 200}, "6000"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_shipping_setting(bad)

    def test_the_setting_is_applied_live(self):
        policy = ShopPolicy()
        agent = SimpleNamespace(inbox_skill=SimpleNamespace(
            reply_provider=SimpleNamespace(shop_policy=policy)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text("products: {shipping: null}\n", encoding="utf-8")
        result = apply_runtime_settings(agent, load_config(path), {"products.shipping": SHIPPING})
        self.assertIn("products.shipping", result["applied"])
        self.assertEqual(policy.facts()["shipping_fee"], 6000)


class TotalsAndCurrencyTests(unittest.TestCase):
    DATA = {"price": 124000, "offers": [{"price": 220000}],
            "shop_policy": {"shipping_fee": 6000}}

    def test_a_total_built_from_verified_amounts_passes(self):
        for text in (
            "Một hộp 124.000 kyats, ship 6.000 kyats, tổng 130.000 kyats.",
            "2 ဘူး ၂၄၈,၀၀၀ ကျပ်",
            "Combo 220,000 Ks",
        ):
            with self.subTest(text=text):
                self.assertFalse(priced(text, self.DATA))

    def test_an_invented_amount_is_refused_in_kyats_too(self):
        self.assertTrue(priced("Chỉ 99.000 kyats thôi ạ", self.DATA))
        self.assertTrue(priced("125,000 kyat", self.DATA))

    def test_the_shipping_fee_alone_is_not_a_unit_price(self):
        """6,000 is verified; 12,000 (two fees) is not a quantity x price."""
        self.assertTrue(priced("Phí 12.000 kyats", {"shop_policy": {"shipping_fee": 6000}}))


class ComboOffersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        products = {
            "cg": {"name": "Calcium Gold", "price": 124000, "stock": 50},
            "cg-5": {"name": "Combo 5 - mua 5 tặng 2", "price": 660000, "item_type": "combo",
                     "combo_items": [{"product_id": "cg", "quantity": 7}], "cost_price": 1},
            "cg-2": {"name": "Combo 2", "price": 220000, "item_type": "combo",
                     "combo_items": [{"product_id": "cg", "quantity": 2}]},
            "cg-old": {"name": "Combo cũ", "price": 100000, "item_type": "combo",
                       "is_active": False, "combo_items": [{"product_id": "cg", "quantity": 1}]},
        }
        for product_id, product in products.items():
            (root / product_id).mkdir()
            (root / product_id / "product.json").write_text(
                json.dumps({"id": product_id, **product}), encoding="utf-8")
        self.repository = ProductRepository(root)

    async def test_a_product_lists_its_active_combos_cheapest_first(self):
        product = await self.repository.get_product("cg")
        self.assertEqual([offer["id"] for offer in product["offers"]], ["cg-2", "cg-5"])
        self.assertEqual(product["offers"][1]["items"], [{"product_id": "cg", "quantity": 7}])
        self.assertNotIn("cost_price", json.dumps(product))

    async def test_a_combo_lists_no_offers(self):
        self.assertEqual((await self.repository.get_product("cg-2"))["offers"], [])


class RoutingConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_order_status_goes_to_a_person(self):
        router = MessageRouter(human_intents=self.config.get("routing.human_intents"))
        self.assertIs(router.route(analysis(intent="order_status")), MessageRoute.HUMAN)

    def test_product_feedback_is_registered_and_stays_with_the_agent(self):
        self.assertIn("product_feedback", self.config.get("routing.registered_intents"))
        router = MessageRouter(human_intents=self.config.get("routing.human_intents"))
        self.assertIsNot(router.route(analysis(intent="product_feedback")), MessageRoute.HUMAN)


class RecordingGenerator:
    reply_language = "vi"

    def __init__(self, text="Dạ shop đã nhận đủ thông tin, nhân viên sẽ xác nhận đơn ạ."):
        self.text = text
        self.tool_results = []

    async def generate(self, event, state, analysis, tool_result=None,
                       attachments=(), unavailable_products=(), notes=()):
        self.tool_results.append(tool_result)
        return ReplyResult(text=self.text, should_send=True, model="stub")


class FactsStore(StubStore):
    def __init__(self, facts):
        super().__init__()
        self.facts = facts

    async def context(self, conversation_id, limit):
        return {"customer_facts": {key: {"value": value} for key, value in self.facts.items()}}


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, store, *, intent="ask_order", next_stage="ordering", generator=None,
                 shipping=None, order_handoff=True):
        return PipelineReplyProvider(
            analyzer=StubAnalyzer(analysis(intent=intent, next_stage=next_stage)),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=generator or RecordingGenerator(),
            reply_validator=ReplyValidator(500),
            conversation_store=store,
            shop_policy=ShopPolicy(shipping),
            order_handoff=order_handoff,
        )

    def context(self, text="Giao về số 12 Bogyoke, SĐT 09123456"):
        return ConversationContext(
            platform="tiktok_web", conversation_key="thread-1", message_id="m1",
            message_text=text, customer_name="khách",
            history=[{"id": "m1", "text": text, "direction": "INBOUND"}],
        )

    async def test_delivery_facts_reach_the_generator_without_a_product_lookup(self):
        generator = RecordingGenerator(text="Phí ship 6.000 kyats, giao 2-5 ngày ạ.")
        decision = await self.provider(
            StubStore(), intent="faq", next_stage="browsing", generator=generator,
            shipping=SHIPPING,
        ).reply(self.context("Ship bao nhiêu shop?"))
        self.assertEqual(generator.tool_results[0].data["shop_policy"]["shipping_fee"], 6000)
        self.assertTrue(decision.should_send)
        self.assertEqual(decision.reason, "generated")

    async def test_without_delivery_facts_a_quoted_fee_is_refused(self):
        generator = RecordingGenerator(text="Phí ship 6.000 kyats ạ.")
        decision = await self.provider(
            StubStore(), intent="faq", next_stage="browsing", generator=generator,
        ).reply(self.context("Ship bao nhiêu shop?"))
        self.assertTrue(decision.requires_human)
        self.assertEqual(decision.reason, "unverified_price_claim")

    async def test_an_order_with_phone_and_address_goes_to_a_person_once(self):
        store = FactsStore({"phone": "09123456", "address": "12 Bogyoke, Yangon"})
        provider = self.provider(store)

        first = await provider.reply(self.context())
        self.assertTrue(first.should_send)
        self.assertTrue(first.requires_human)
        self.assertEqual(first.reason, ORDER_READY_REASON)

        # The same order, the customer writing again: not a second handoff.
        store.state = first.state
        second = await provider.reply(self.context("Cảm ơn shop"))
        self.assertFalse(second.requires_human)

    async def test_no_handoff_while_the_phone_is_missing(self):
        decision = await self.provider(
            FactsStore({"address": "12 Bogyoke, Yangon"}),
        ).reply(self.context())
        self.assertFalse(decision.requires_human)

    async def test_no_handoff_outside_an_order(self):
        decision = await self.provider(
            FactsStore({"phone": "09123456", "address": "12 Bogyoke"}),
            intent="faq", next_stage="browsing",
        ).reply(self.context("Sữa pha thế nào?"))
        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)

    async def test_the_handoff_can_be_switched_off(self):
        decision = await self.provider(
            FactsStore({"phone": "09123456", "address": "12 Bogyoke"}), order_handoff=False,
        ).reply(self.context())
        self.assertFalse(decision.requires_human)


class ShippingRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import shutil
        from unittest import mock

        from dashboard_api import main

        self.main = main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"
        shutil.copy(Path(__file__).resolve().parents[1] / "configs" / "config.yaml", self.path)
        account = SimpleNamespace(id="acc", control_port=28999)
        self.control = mock.AsyncMock(return_value={})
        for target, value in (
            ("registry", mock.Mock(config=lambda account: load_config(self.path))),
            ("_account_or_404", lambda account_id: account),
            ("_control", self.control),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_valid_form_is_saved_and_pushed_live(self):
        body = await self.main.write_shipping("acc", shipping=dict(SHIPPING))
        self.assertEqual(body["shipping"], SHIPPING)
        self.assertEqual(load_config(self.path).get("products.shipping"), SHIPPING)
        self.assertEqual(self.main.read_shipping("acc")["shipping"], SHIPPING)
        self.control.assert_awaited_once()

    async def test_an_invalid_form_is_refused_and_nothing_changes(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as raised:
            await self.main.write_shipping("acc", shipping={"fee": -1})
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIsNone(load_config(self.path).get("products.shipping"))


class NotificationTests(unittest.TestCase):
    def notify(self, reason):
        store = NotificationStore("postgresql://unused")
        recorded = []
        store.record = lambda **kwargs: recorded.append(kwargs)
        store.from_event({"type": "escalation.opened", "account_id": "shop-a",
                          "payload": {"conversation": "Lan", "reason": reason}})
        return recorded[0]

    def test_an_order_ready_notification_says_to_create_the_order(self):
        notification = self.notify(f"reply_withheld:{ORDER_READY_REASON}")
        self.assertIn("Đơn hàng đủ thông tin", notification["title"])
        self.assertIn("Lan", notification["title"])
        self.assertIn("tạo đơn", notification["detail"])

    def test_an_order_status_question_reads_as_one(self):
        notification = self.notify("routed_to_human:order_status")
        self.assertIn("tình trạng đơn", notification["title"])


if __name__ == "__main__":
    unittest.main()
