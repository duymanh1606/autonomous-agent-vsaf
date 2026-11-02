"""A product deleted from the catalogue is not advised on from memory.

Reported by testing: after a product was deleted, the agent kept describing
and recommending it. The catalogue itself forgot it at once (every read
checks the file on disk), and a lookup for it fails and goes to a person -
but the conversation remembered it: `state.product_id/product_name` carry
over between turns and the history holds earlier replies describing it, so a
question needing no lookup ("tư vấn thêm về áo đó") was answered from memory.

Now each conversation remembers which products the catalogue confirmed, and
every turn re-checks them; a deleted one is dropped as the current product,
named to the model as no longer sold, and a reply that still describes it is
refused.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_service import PipelineReplyProvider
from agent.reply_validator import ReplyValidator, mentions_removed_product
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult
from models.tool import ToolResult


class Catalogue:
    def __init__(self, **products):
        self.products = products

    def get_by_id(self, product_id):
        return self.products.get(product_id)


def _provider(catalogue):
    return PipelineReplyProvider(
        analyzer=None, router=None, state_manager=None,
        reply_generator=None, reply_validator=None, catalogue=catalogue,
    )


def _state():
    return CustomerState(conversation_id="c", customer_id="c")


def _lookup(product_id="ao-kg", name="Áo khoác gió"):
    return ToolResult(tool_name="get_product", success=True, data={"id": product_id, "name": name})


class MemoryTests(unittest.TestCase):
    def test_the_reported_case_a_deleted_product_is_forgotten(self):
        catalogue = Catalogue(**{"ao-kg": {"id": "ao-kg", "name": "Áo khoác gió"}})
        provider = _provider(catalogue)
        state = _state()
        self.assertTrue(provider._remember_verified_product(state, _lookup()))
        state.product_id, state.product_name = "ao-kg", "Áo khoác gió"
        self.assertEqual(provider._forget_removed_products(state), [])

        del catalogue.products["ao-kg"]  # the shop deletes it

        self.assertEqual(provider._forget_removed_products(state), ["Áo khoác gió"])
        self.assertIsNone(state.product_id)
        self.assertIsNone(state.product_name)
        # Still named on later turns, not only the first after deletion.
        self.assertEqual(provider._forget_removed_products(state), ["Áo khoác gió"])

    def test_taken_off_sale_counts_as_gone_and_coming_back_undoes_it(self):
        product = {"id": "ao-kg", "name": "Áo khoác gió", "is_active": False}
        catalogue = Catalogue(**{"ao-kg": product})
        provider = _provider(catalogue)
        state = _state()
        provider._remember_verified_product(state, _lookup())
        self.assertEqual(provider._forget_removed_products(state), ["Áo khoác gió"])
        product["is_active"] = True
        self.assertEqual(provider._forget_removed_products(state), [])

    def test_a_vague_product_the_catalogue_never_confirmed_is_not_flagged(self):
        """"áo đó" in state is not a deleted product; only confirmed ones are checked."""
        provider = _provider(Catalogue())
        state = _state()
        state.product_id = state.product_name = "áo đó"
        self.assertEqual(provider._forget_removed_products(state), [])
        self.assertEqual(state.product_name, "áo đó")

    def test_only_a_successful_product_lookup_is_remembered(self):
        provider = _provider(Catalogue())
        state = _state()
        failed = ToolResult(tool_name="get_product", success=False, error="x")
        shipping = ToolResult(tool_name="get_shipping", success=True, data={"id": "gh"})
        self.assertFalse(provider._remember_verified_product(state, failed))
        self.assertFalse(provider._remember_verified_product(state, shipping))
        self.assertNotIn("verified_products", state.metadata)


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_is_told_and_history_is_not_a_source_of_facts(self):
        prompts = []

        class LLM:
            model_name = "m"

            async def generate_structured(self, prompt, schema):
                prompts.append(prompt)
                return ReplyResult(text="Dạ sản phẩm này shop đã ngừng bán ạ", should_send=True)

        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Tư vấn thêm về áo khoác gió với shop",
        )
        analysis = MessageAnalysis(intent="ask_product", next_stage="asking_product", confidence=0.9)
        await ReplyGenerator(LLM(), 500, ProductLookupPolicy(True)).generate(
            event, _state(), analysis, unavailable_products=["Áo khoác gió"],
        )
        self.assertIn('No longer sold by the shop: ["Áo khoác gió"]', prompts[0])
        self.assertIn("Earlier messages and the conversation summary can be out of date", prompts[0])

    def test_a_reply_still_advising_on_it_is_refused(self):
        advising = "Áo khoác gió chất liệu dù chống nước, giá rất tốt ạ"
        self.assertTrue(mentions_removed_product(advising, ["Áo khoác gió"]))
        result = ReplyValidator().validate(
            ReplyResult(text=advising, should_send=True),
            MessageAnalysis(intent="ask_product", next_stage="asking_product", confidence=0.9),
            tool_result=None, last_outbound_text=None, removed_products=["Áo khoác gió"],
        )
        self.assertEqual(result.reason, "mentions_removed_product")

    def test_saying_it_is_no_longer_sold_is_allowed(self):
        for text in (
            "Dạ áo khoác gió shop đã ngừng bán rồi ạ",
            "Dạ mẫu Áo khoác gió hiện không còn kinh doanh ạ",
            "Dạ shop còn áo hoodie, bạn xem thử nhé",  # does not name it at all
        ):
            with self.subTest(text=text):
                self.assertFalse(mentions_removed_product(text, ["Áo khoác gió"]))


if __name__ == "__main__":
    unittest.main()
