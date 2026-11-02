"""Live, non-sending evaluation of a `reply_language: my` shop on the Burmese
customer-care dataset (data/testcase-myanmar/*.xlsx, see tests/myanmar_dataset.py).

The workbook has 1000 rows but far fewer distinct customer messages; each
distinct message is answered once through the real pipeline (Gemini analyse ->
route -> generate -> validate) and the verdict applies to every row that
carries it. Stops at ReplyDecision: no TikTok, nothing is sent.

Graded on what the code must guarantee, not on wording - the dataset's
expected replies are synthetic and unreviewed, so they go into the report for
a person to compare, never into the verdict:
  - anything sent is in Burmese (the handoff ack included);
  - a complaint reaches a person;
  - nobody is sent media, since no message asks for any;
  - every message gets a terminal decision (send or handoff).

Run: uv run python tests/gemini_myanmar_dataset_evaluation.py
"""

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_language import is_wrong_language
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from load_config import load_config
from storage.product_repository import ProductRepository
from tests.myanmar_dataset import dataset_path, load_cases
from tools.business import register_business_services
from tools.registry import BusinessToolRegistry

MODEL = "gemini-3.1-flash-lite"
REPORT = Path("data/test-results/gemini-myanmar-dataset.json")


def grade(intent, row):
    failures = []
    text = row["text"]
    if not (row["should_send"] or row["requires_human"]):
        failures.append("no_terminal_decision")
    if text and is_wrong_language("my", text):
        failures.append("not_burmese")
    if row["attachments"] or row["requested_media"]:
        failures.append("unrequested_media")
    if intent == "complaint" and not row["requires_human"]:
        failures.append("complaint_not_handed_over")
    return failures


def compact(decision):
    analysis = decision.analysis
    return {
        "should_send": decision.should_send,
        "requires_human": decision.requires_human,
        "reason": decision.reason,
        "model": decision.model,
        "text": decision.text,
        "intent": getattr(analysis, "intent", None),
        "tool": getattr(analysis, "tool_name", None),
        "requested_media": list(getattr(analysis, "requested_media", []) or []),
        "attachments": [
            {"kind": item.kind, "name": item.name} for item in decision.attachments
        ],
    }


async def main():
    cases = load_cases()
    if not cases:
        sys.exit(f"Myanmar dataset not found ({dataset_path()})")
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set")

    # One evaluation per distinct (intent, message): the same text under two
    # intents would be graded differently.
    groups = defaultdict(list)
    for case in cases:
        groups[(case.intent, case.customer_message)].append(case)

    config = load_config()
    client = GeminiFlashLiteProvider(
        api_key=os.environ["GEMINI_API_KEY"],
        model=MODEL,
        base_url=config.get("llm.base_url"),
        timeout_seconds=60,
    )
    tools = BusinessToolRegistry(["get_product", "check_inventory"])
    products = ProductRepository(config.get("products.product_root"))
    register_business_services(tools, product=products, inventory=products)
    stages = config.get("routing.registered_stages")
    lookup = ProductLookupPolicy(True)

    def provider():
        # A fresh chain per message: the unknown-intent counter and the
        # last-outbound memory must not carry from one customer to the next.
        return PipelineReplyProvider(
            analyzer=MessageAnalyzer(
                client, config.get("routing.registered_intents"), stages, tools.names,
            ),
            router=MessageRouter(
                config.get("routing.strong_llm_threshold", 0.65),
                config.get("routing.human_intents", []),
            ),
            state_manager=CustomerStateManager(stages),
            reply_generator=ReplyGenerator(client, 1000, lookup, reply_language="my"),
            reply_validator=ReplyValidator(1000),
            business_tools=tools,
            product_lookup=lookup,
            enabled_media_kinds=("image", "video"),
        )

    semaphore = asyncio.Semaphore(3)

    async def run(index, key):
        intent, message = key
        async with semaphore:
            try:
                decision = await provider().reply(ConversationContext(
                    platform="tiktok_web",
                    conversation_key=f"mm-eval-{index}",
                    message_id=f"mm-eval-{index}",
                    message_text=message,
                    history=[],
                ))
                row = compact(decision)
                failures = grade(intent, row)
                row["status"] = "FAIL" if failures else "PASS"
                row["failures"] = failures
            except Exception as exc:
                row = {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}
        members = groups[key]
        row.update({
            "dataset_intent": intent,
            "customer_message": message,
            "rows": len(members),
            "test_ids": sorted(case.test_id for case in members),
            "reference_replies_mm": sorted({case.expected_reply_mm for case in members})[:3],
        })
        return row

    try:
        results = await asyncio.gather(*(
            run(index, key) for index, key in enumerate(sorted(groups))
        ))
    finally:
        await client.close()

    distinct = Counter(row["status"] for row in results)
    rows = Counter()
    for row in results:
        rows[row["status"]] += row["rows"]
    by_intent = defaultdict(Counter)
    for row in results:
        by_intent[row["dataset_intent"]][row["status"]] += row["rows"]
    failure_kinds = Counter(kind for row in results for kind in row.get("failures", ()))

    report = {
        "model": MODEL,
        "reply_language": "my",
        "sends_real_tiktok_messages": False,
        "dataset": str(dataset_path()),
        "distinct_messages": dict(distinct),
        "dataset_rows": dict(rows),
        "by_intent": {intent: dict(counts) for intent, counts in sorted(by_intent.items())},
        "failure_kinds": dict(failure_kinds),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(REPORT),
        "distinct_messages": dict(distinct),
        "dataset_rows": dict(rows),
        "failure_kinds": dict(failure_kinds),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
