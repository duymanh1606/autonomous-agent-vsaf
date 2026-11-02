"""`products.lookup_enabled` decides whether product facts come from the DB."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_language import default_reply_style
from agent.reply_generator import ReplyGenerator
from agent.scenario_policy import ScenarioPolicy
from agent.reply_service import PipelineReplyProvider
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult
from models.tool import ToolResult



def _analysis(**overrides):
    data = {
        "intent": "ask_product",
        "next_stage": "asking_product",
        "requires_tool": True,
        "tool_name": "get_product",
        "tool_args": {"product_id": "VENUS_BIGONE_001"},
        "confidence": 0.9,
    }
    data.update(overrides)
    return MessageAnalysis(**data)


class RecordingLLM:
    model_name = "test-model"

    def __init__(self):
        self.prompt = ""

    async def generate_structured(self, prompt, schema):
        self.prompt = prompt
        return ReplyResult(text="ok", should_send=True)


class ProductLookupFlag(unittest.TestCase):
    def test_only_verified_catalogue_media_becomes_an_attachment(self):
        selected = PipelineReplyProvider._verified_attachments(
            _analysis(requested_media="image"),
            ToolResult(tool_name="get_product", success=True, data={
                "media": {"images": [
                    {"path": "/tmp/missing.jpg", "exists": False, "valid_type": True},
                    {"path": "/catalogue/front.jpg", "name": "front.jpg",
                     "exists": True, "valid_type": True},
                ]}
            }),
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].path, "/catalogue/front.jpg")

    def test_verified_catalogue_video_becomes_a_video_attachment(self):
        selected = PipelineReplyProvider._verified_attachments(
            _analysis(requested_media="video"),
            ToolResult(tool_name="get_product", success=True, data={
                "media": {"videos": [{
                    "path": "/catalogue/intro.mp4", "name": "intro.mp4",
                    "exists": True, "valid_type": True,
                }]}
            }),
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].kind, "video")
        self.assertEqual(selected[0].path, "/catalogue/intro.mp4")

    def test_image_and_video_request_returns_one_verified_file_of_each_kind(self):
        selected = PipelineReplyProvider._verified_attachments(
            _analysis(requested_media=["image", "video"]),
            ToolResult(tool_name="get_product", success=True, data={"media": {
                "images": [{
                    "path": "/catalogue/front.jpg", "name": "front.jpg",
                    "exists": True, "valid_type": True,
                }],
                "videos": [{
                    "path": "/catalogue/intro.mp4", "name": "intro.mp4",
                    "exists": True, "valid_type": True,
                }],
            }}),
        )
        self.assertEqual([item.kind for item in selected], ["image", "video"])

    def test_enabled_keeps_the_tool_request(self):
        analysis = ProductLookupPolicy(True).apply(_analysis())
        self.assertIs(analysis.requires_tool, True)
        self.assertEqual(analysis.tool_name, "get_product")
        self.assertEqual(MessageRouter().route(analysis).value, "TOOL")

    def test_disabled_drops_the_tool_and_routes_to_the_llm(self):
        analysis = ProductLookupPolicy(False).apply(_analysis())
        self.assertIs(analysis.requires_tool, False)
        self.assertIsNone(analysis.tool_name)
        self.assertEqual(analysis.tool_args, {})
        self.assertEqual(MessageRouter().route(analysis).value, "DIRECT")

    def test_disabled_leaves_non_product_tools_alone(self):
        analysis = ProductLookupPolicy(False).apply(
            _analysis(intent="ask_order", tool_name="get_order", tool_args={"order_id": "1"})
        )
        self.assertIs(analysis.requires_tool, True)
        self.assertEqual(analysis.tool_name, "get_order")

    def test_product_name_argument_is_canonicalised_for_the_repository(self):
        analysis = ProductLookupPolicy(True).apply(_analysis(
            tool_args={"product_name": "sữa milo"},
            entities={"product_name": "sữa milo"},
        ))
        self.assertEqual(analysis.tool_name, "get_product")
        self.assertEqual(analysis.tool_args, {"product_id": "sữa milo"})

    def test_named_price_question_is_forced_through_verified_lookup(self):
        analysis = ProductLookupPolicy(True).apply(_analysis(
            requires_tool=False,
            tool_name=None,
            tool_args={},
            intent="ask_price",
            entities={"product_name": "Milo"},
        ))
        self.assertTrue(analysis.requires_tool)
        self.assertEqual(analysis.tool_name, "get_product")
        self.assertEqual(analysis.tool_args, {"product_id": "Milo"})

    def test_explicit_image_request_is_forced_through_verified_lookup(self):
        analysis = ProductLookupPolicy(True).apply(_analysis(
            requires_tool=False,
            tool_name=None,
            tool_args={},
            requested_media="image",
            entities={"product_name": "Milo"},
        ))
        self.assertTrue(analysis.requires_tool)
        self.assertEqual(analysis.tool_name, "get_product")
        self.assertEqual(analysis.tool_args, {"product_id": "Milo"})

    def test_inventory_keeps_only_supported_arguments(self):
        analysis = ProductLookupPolicy(True).apply(_analysis(
            intent="check_inventory",
            tool_name="check_inventory",
            tool_args={"product_name": "Milo", "size": "M", "unused": "x"},
            entities={"product_name": "Milo", "size": "M"},
        ))
        self.assertEqual(analysis.tool_args, {"product_id": "Milo", "size": "M"})


class ProductPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_switches_to_context_only_when_disabled(self):
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Sản phẩm này giá bao nhiêu?",
        )
        state = CustomerState(conversation_id="c", customer_id="c")

        off_llm = RecordingLLM()
        await ReplyGenerator(off_llm, 500, ProductLookupPolicy(False)).generate(
            event, state, ProductLookupPolicy(False).apply(_analysis())
        )
        self.assertIn("not being queried", off_llm.prompt)
        self.assertNotIn("verified_tool_result", off_llm.prompt)

        on_llm = RecordingLLM()
        await ReplyGenerator(on_llm, 500, ProductLookupPolicy(True)).generate(
            event, state, _analysis()
        )
        self.assertIn("verified_tool_result", on_llm.prompt)

    async def test_media_paths_are_stripped_from_the_prompt(self):
        """A model shown the real on-disk media path/filename for a product
        photo has been observed inventing a plausible-looking (but fake)
        public URL for it instead of the local path it was told not to write
        - e.g. `https://cdn.example.com/images/<real-filename>.png`. The
        verified attachment pipeline reads `media` straight from
        `tool_result.data` in Python (see `_verified_attachments` in
        reply_service.py), never from anything the model writes, so the
        prompt must never hand it the raw path material to hallucinate from."""
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Cho em xem ảnh Gà Rán Jollibee với ạ",
        )
        state = CustomerState(conversation_id="c", customer_id="c")
        tool_result = ToolResult(tool_name="get_product", success=True, data={
            "id": "sp007",
            "name": "Gà Rán",
            "brand": "Jollibee",
            "media": {
                "images": [{
                    "type": "image", "name": "f3a43eedabdb41fd.png",
                    "relative_path": "images/f3a43eedabdb41fd.png",
                    "path": "/srv/data/accounts/acct-a/products/sp007/images/f3a43eedabdb41fd.png",
                    "exists": True, "valid_type": True, "size": 1024,
                }],
                "videos": [],
            },
        })

        llm = RecordingLLM()
        await ReplyGenerator(llm, 500, ProductLookupPolicy(True)).generate(
            event, state, _analysis(requested_media="image"), tool_result,
        )

        self.assertIn("Gà Rán", llm.prompt)
        self.assertNotIn("f3a43eedabdb41fd", llm.prompt)
        self.assertNotIn("images/", llm.prompt)
        self.assertNotIn("/srv/data", llm.prompt)


class ScenarioPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_scenario_examples_reach_the_prompt_when_set(self):
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Sản phẩm này giá bao nhiêu?",
        )
        state = CustomerState(conversation_id="c", customer_id="c")
        scenario = ScenarioPolicy([
            {"customer": "Sản phẩm này giá bao nhiêu?",
             "agent": "Dạ chào bạn, sản phẩm này giá 199k ạ."},
        ])

        llm = RecordingLLM()
        await ReplyGenerator(
            llm, 500, ProductLookupPolicy(True), scenario_policy=scenario,
        ).generate(event, state, _analysis())

        self.assertIn("Dạ chào bạn, sản phẩm này giá 199k ạ.", llm.prompt)
        # Guidance only - the guardrail against inventing facts must still
        # appear, and appear regardless of what the operator wrote.
        self.assertIn("never use them to state a price", llm.prompt)

    async def test_empty_scenario_adds_nothing_to_the_prompt(self):
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Sản phẩm này giá bao nhiêu?",
        )
        state = CustomerState(conversation_id="c", customer_id="c")

        llm = RecordingLLM()
        await ReplyGenerator(llm, 500, ProductLookupPolicy(True)).generate(
            event, state, _analysis()
        )
        self.assertNotIn("Shop-provided example", llm.prompt)

    async def test_multiple_examples_are_all_numbered_into_the_prompt(self):
        scenario = ScenarioPolicy([
            {"customer": "Còn hàng không shop?", "agent": "Dạ còn bạn nhé."},
            {"customer": "Ship bao lâu vậy shop?", "agent": "Dạ 2-3 ngày ạ."},
        ])
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Hi",
        )
        state = CustomerState(conversation_id="c", customer_id="c")

        llm = RecordingLLM()
        await ReplyGenerator(
            llm, 500, ProductLookupPolicy(True), scenario_policy=scenario,
        ).generate(event, state, _analysis())

        self.assertIn("Example 1 customer: Còn hàng không shop?", llm.prompt)
        self.assertIn("Example 2 customer: Ship bao lâu vậy shop?", llm.prompt)

    async def test_editing_the_shared_policy_changes_both_generators_prompt(self):
        # main.py hands the same ScenarioPolicy instance to the weak and the
        # strong-LLM ReplyGenerator, exactly like ProductLookupPolicy - one
        # dashboard edit (control/settings.py's setter calls `.set()`) must
        # reach whichever one answers the next message.
        scenario = ScenarioPolicy()
        weak_llm, strong_llm = RecordingLLM(), RecordingLLM()
        weak = ReplyGenerator(weak_llm, 500, ProductLookupPolicy(True), scenario_policy=scenario)
        strong = ReplyGenerator(strong_llm, 500, ProductLookupPolicy(True), scenario_policy=scenario)

        scenario.set([{"customer": "Hi", "agent": "Dạ chào anh/chị ạ."}])

        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Hi",
        )
        state = CustomerState(conversation_id="c", customer_id="c")
        await weak.generate(event, state, _analysis())
        await strong.generate(event, state, _analysis())

        self.assertIn("Dạ chào anh/chị ạ.", weak_llm.prompt)
        self.assertIn("Dạ chào anh/chị ạ.", strong_llm.prompt)

    def test_examples_are_clipped_to_the_field_character_cap(self):
        policy = ScenarioPolicy([{"customer": "x" * 5000, "agent": "y" * 5000}])
        self.assertEqual(len(policy.examples[0]["customer"]), 500)
        self.assertEqual(len(policy.examples[0]["agent"]), 500)

    def test_examples_are_capped_in_count(self):
        policy = ScenarioPolicy([
            {"customer": str(i), "agent": str(i)} for i in range(30)
        ])
        self.assertEqual(len(policy.examples), 20)

    def test_a_one_sided_example_is_dropped(self):
        policy = ScenarioPolicy([{"customer": "Hi", "agent": ""}])
        self.assertEqual(policy.examples, [])


if __name__ == "__main__":
    unittest.main()


class ReplyStylePrompt(unittest.IsolatedAsyncioTestCase):
    def _event_and_state(self):
        event = MessageEvent(
            account_id="a", platform="tiktok", conversation_id="c", customer_id="c",
            message_id="m", message="Sản phẩm này giá bao nhiêu?",
        )
        return event, CustomerState(conversation_id="c", customer_id="c")

    async def test_the_language_default_rules_reach_the_prompt(self):
        """Nothing configured: the reply language's own rules
        (agent/reply_language.py) are what shape the reply."""
        event, state = self._event_and_state()
        for language in ("vi", "my"):
            with self.subTest(language=language):
                llm = RecordingLLM()
                await ReplyGenerator(
                    llm, 500, ProductLookupPolicy(True), reply_language=language,
                ).generate(event, state, _analysis())
                self.assertIn("Style rules (mandatory):", llm.prompt)
                for rule in default_reply_style(language):
                    self.assertIn(f"- {rule}", llm.prompt)

    async def test_a_burmese_shop_gets_no_vietnamese_style_rules(self):
        event, state = self._event_and_state()
        llm = RecordingLLM()
        await ReplyGenerator(
            llm, 500, ProductLookupPolicy(True), reply_language="my",
        ).generate(event, state, _analysis())
        block = llm.prompt.split("Style rules (mandatory):", 1)[1].split("\nMessage:", 1)[0]
        for vietnamese in ("Dạ", "anh/chị", "xưng"):
            self.assertNotIn(vietnamese, block)

    async def test_default_rules_follow_a_language_switch(self):
        generator = ReplyGenerator(RecordingLLM(), 500, ProductLookupPolicy(True))
        self.assertIn("anh/chị", generator.style_block)
        generator.reply_language = "my"
        self.assertNotIn("anh/chị", generator.style_block)
        self.assertIn("ရှင်", generator.style_block)

    async def test_an_operators_own_rules_do_not_follow_the_language(self):
        generator = ReplyGenerator(
            RecordingLLM(), 500, ProductLookupPolicy(True), reply_style=["Xưng mình."],
        )
        generator.reply_language = "my"
        self.assertIn("- Xưng mình.", generator.style_block)

    async def test_configured_style_is_rendered_and_blank_rules_dropped(self):
        event, state = self._event_and_state()
        llm = RecordingLLM()
        await ReplyGenerator(
            llm, 500, ProductLookupPolicy(True),
            reply_style=["Giọng thân mật, xưng mình - gọi bạn.", "  "],
        ).generate(event, state, _analysis())
        self.assertIn("- Giọng thân mật, xưng mình - gọi bạn.", llm.prompt)
        self.assertEqual(llm.prompt.count("\n- "), 1)
        # The fact guardrail is independent of the style block.
        self.assertIn("Do not invent product", llm.prompt)

    async def test_an_emptied_style_adds_no_block(self):
        """[] is an operator's choice of no rules, unlike an unset style."""
        event, state = self._event_and_state()
        llm = RecordingLLM()
        await ReplyGenerator(llm, 500, ProductLookupPolicy(True), reply_style=[]).generate(
            event, state, _analysis()
        )
        self.assertNotIn("Style rules", llm.prompt)
