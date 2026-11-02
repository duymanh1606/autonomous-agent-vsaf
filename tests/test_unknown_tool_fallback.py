"""A business tool the registry cannot execute must not silence the agent.

The analyzer names its tool in free text, so it can ask for one that was never
allowlisted (`get_product_price` instead of `get_product`). That request used
to fail inside the registry and hand the conversation to a human, which is
what made the agent read messages without ever answering them.

No API key is used: the LLM and the tools are stubs.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult
from tools.registry import BusinessToolRegistry


INTENTS = ("ask_price", "ask_product", "ask_order", "unknown")
STAGES = ("new", "asking_price", "asking_product", "human_required")


def analysis(**overrides) -> MessageAnalysis:
    data = {
        "intent": "ask_price",
        "next_stage": "asking_price",
        "requires_tool": True,
        "tool_name": "get_product_price",
        "tool_args": {"product_name": "áo thun"},
        "confidence": 0.9,
    }
    data.update(overrides)
    return MessageAnalysis(**data)


def event() -> MessageEvent:
    return MessageEvent(
        account_id="default", platform="tiktok_web", conversation_id="c1",
        customer_id="c1", message_id="m1", message="Áo này giá bao nhiêu shop?",
    )


def state() -> CustomerState:
    return CustomerState(conversation_id="c1", customer_id="c1")


class StubLLM:
    """Returns one canned structured answer and remembers the prompt."""

    model_name = "gemini-3.1-flash-lite"

    def __init__(self, result):
        self.result = result
        self.prompt = ""

    async def generate_structured(self, prompt, schema):
        self.prompt = prompt
        return self.result.model_copy(deep=True)


class StubAnalyzer:
    def __init__(self, result):
        self.result = result

    async def analyze(self, event, state, messages, customer_context=None):
        return self.result.model_copy(deep=True)


class StubGenerator:
    async def generate(self, event, state, analysis, tool_result=None, attachments=(), unavailable_products=(), notes=()):
        return ReplyResult(text="Áo thun bên em còn hàng ạ.", should_send=True, model="stub")


class NeverCalledTool:
    name = "get_product"

    async def execute(self, arguments):
        raise AssertionError("a dropped tool request must never be executed")


def registry() -> BusinessToolRegistry:
    tools = BusinessToolRegistry(["get_product"])
    tools.register(NeverCalledTool())
    return tools


class AnalyzerKnowsItsTools(unittest.IsolatedAsyncioTestCase):
    """1 + 2: the prompt lists the tools, and unknown names are dropped."""

    async def test_prompt_lists_the_registered_tools(self):
        llm = StubLLM(analysis(requires_tool=False, tool_name=None, tool_args={}))
        await MessageAnalyzer(llm, INTENTS, STAGES, registry().names).analyze(
            event(), state(), []
        )
        self.assertIn("Registered tools", llm.prompt)
        self.assertIn('"get_product"', llm.prompt)
        self.assertIn("Tool argument schemas", llm.prompt)
        self.assertIn('"product_id"', llm.prompt)

    async def test_an_unknown_tool_name_is_dropped(self):
        llm = StubLLM(analysis())
        result = await MessageAnalyzer(llm, INTENTS, STAGES, registry().names).analyze(
            event(), state(), []
        )
        self.assertFalse(result.requires_tool)
        self.assertIsNone(result.tool_name)
        self.assertEqual(result.tool_args, {})
        self.assertNotEqual(MessageRouter().route(result).value, "TOOL")

    async def test_a_registered_tool_survives(self):
        llm = StubLLM(analysis(tool_name="get_product", tool_args={"product_id": "1"}))
        result = await MessageAnalyzer(llm, INTENTS, STAGES, registry().names).analyze(
            event(), state(), []
        )
        self.assertTrue(result.requires_tool)
        self.assertEqual(result.tool_name, "get_product")
        self.assertEqual(MessageRouter().route(result).value, "TOOL")


class UnknownToolStillGetsAnAnswer(unittest.IsolatedAsyncioTestCase):
    """3: the reply path does not escalate because of a tool that does not exist."""

    async def test_reply_provider_answers(self):
        provider = PipelineReplyProvider(
            analyzer=StubAnalyzer(analysis()),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=StubGenerator(),
            reply_validator=ReplyValidator(500),
            business_tools=registry(),
            product_lookup=ProductLookupPolicy(True),
        )
        decision = await provider.reply(ConversationContext(
            platform="tiktok_web",
            conversation_key="c1",
            message_id="m1",
            message_text="Áo này giá bao nhiêu shop?",
        ))
        self.assertTrue(decision.should_send)
        self.assertFalse(decision.requires_human)
        self.assertEqual(decision.reason, "generated")


if __name__ == "__main__":
    unittest.main()
