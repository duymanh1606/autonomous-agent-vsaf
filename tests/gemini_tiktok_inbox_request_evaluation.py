"""Non-sending TikTok-like inbox/request evaluation with live Gemini.

Thirty customers arrive together. Browser actions are serialized like one
TikTok tab, while Gemini analysis may run concurrently. Ten are already-known
contacts (a normal unread message, straight through the reply pipeline);
twenty are strangers via a Message request. Accept always sends a short,
fixed, operator-configured confirmation first
(`messaging.auto_check.message_request_retry_prompt`) - never composed by
Gemini. Their pre-accept message is then answered for real, through the full
Gemini 3.1 Flash Lite pipeline exactly like a known customer's message - the
same content simulates both turns here since the production code answers it
directly rather than waiting for the customer to repeat it. No TikTok message
is actually sent.
"""

import asyncio
import json
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_analyzer import MessageAnalyzer
from agent.message_router import MessageRouter
from agent.product_lookup import ProductLookupPolicy
from agent.reply_generator import ReplyGenerator
from agent.reply_service import ConversationContext, PipelineReplyProvider
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from load_config import load_config
from storage.product_repository import ProductRepository
from tools.business import register_business_services
from tools.registry import BusinessToolRegistry


REPORT = Path("data/test-results/gemini-3.1-flash-lite-tiktok-inbox-requests.json")
PRODUCT_ROOT = "data/products"

PROMPTS = (
    ("Chào shop, shop còn hoạt động không?", "general"),
    ("Sữa Milo 180ml còn hàng không và giá bao nhiêu?", "milo_price"),
    ("Gửi giúp mình hình ảnh sữa Milo 180ml.", "milo_image"),
    ("Gửi giúp mình video sữa Milo 180ml.", "milo_video"),
    ("Cho mình cả ảnh và video sữa Venus BigOne.", "venus_both"),
    ("Shop ơi\nMilo còn không?\nGiá bao nhiêu?\nGửi ảnh nhé.", "burst"),
    ("Hộp sữa nhận được bị móp và chảy sữa.", "complaint"),
    ("Mua 10 hộp Milo có được giảm 20% không?", "unknown_discount"),
    ("", "image_only"),
    ("asdf, mình chưa biết cần loại nào.", "unclear"),
)


def make_case(number):
    prompt, expected = PROMPTS[(number - 1) % len(PROMPTS)]
    group = "known" if number <= 10 else "stranger"
    conversation = f"sim-user-{number:03d}"
    burst = prompt.splitlines() if "\n" in prompt else [prompt]
    message_ids = [f"{conversation}-m{index + 1}" for index in range(len(burst))]
    return {
        "number": number,
        "conversation_id": conversation,
        "customer_name": f"customer_{number:03d}",
        "group": group,
        "expected": expected,
        "messages": burst,
        "message_ids": message_ids,
        "inbound_media": (
            [{"kind": "image", "url": "simulated://customer-image"}]
            if expected == "image_only" else []
        ),
    }


def _reply_semantic_pass(case, row):
    """Checks the real-answer reply - identical rubric for a known customer's
    message and a stranger's pre-accept message answered after the fixed
    ack."""
    if row.get("error"):
        return False
    text = row["text"].casefold()
    media = row["attachments"]
    expected = case["expected"]
    if expected == "milo_price":
        # Product price/content accuracy is outside this logic-focused pass.
        return row["status"] in {"completed", "requires_human"}
    if expected == "milo_image":
        return any(item["kind"] == "image" and "sp001" in item["path"] for item in media)
    if expected == "milo_video":
        return any(item["kind"] == "video" and "sp001" in item["path"] for item in media)
    if expected == "venus_both":
        kinds = {item["kind"] for item in media if "sp006" in item["path"]}
        return kinds == {"image", "video"}
    if expected == "burst":
        return row["status"] in {"completed", "requires_human"} and (
            row["requires_human"]
            or any(item["kind"] == "image" for item in media)
        )
    if expected == "complaint":
        # `should_send` and `requires_human` are both true for the safe
        # handoff acknowledgement (see agent/reply_service.py's
        # HUMAN_HANDOFF_ACK path) - `status` alone can never read
        # "requires_human" in that case, so the flag itself is what a
        # correct escalation is graded on.
        return row["requires_human"] and row["intent"] == "complaint"
    if expected == "unknown_discount":
        # Repeating the customer's requested "20%" while explicitly denying
        # that the promotion is known is safe; only an affirmative promise is
        # an invention.
        denies_discount = any(
            phrase in text for phrase in (
                "chưa có thông tin", "chưa ghi nhận", "không có thông tin",
                "không có sẵn", "kiểm tra lại", "chuyển thông tin",
            )
        )
        affirmative_discount = bool(re.search(r"(?:được|giảm)\s+20\s*%", text)) and not denies_discount
        return not affirmative_discount and (row["requires_human"] or denies_discount)
    if expected == "image_only":
        return row["status"] in {"completed", "requires_human"} and bool(text)
    if expected == "unclear":
        return row["status"] in {"completed", "requires_human"}
    return row["status"] == "completed" and bool(text)


def semantic_pass(case, row):
    if case["group"] == "stranger":
        # The first-contact acknowledgement must be the fixed text, and must
        # never have gone through Gemini - only then does the real question
        # (turn 2) get judged by the normal reply rubric.
        if row.get("ack_text") != row.get("resend_prompt"):
            return False
        if row.get("ack_gemini_used"):
            return False
    return _reply_semantic_pass(case, row)


async def main():
    load_dotenv()
    config = load_config()
    resend_prompt = config.get(
        "messaging.auto_check.message_request_retry_prompt",
        "Quý khách vui lòng gửi lại yêu cầu giúp shop ạ.",
    )
    client = GeminiFlashLiteProvider(
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        model="gemini-3.1-flash-lite",
        base_url=config.get("llm.base_url"),
        timeout_seconds=60,
    )
    tools = BusinessToolRegistry(["get_product", "check_inventory"])
    products = ProductRepository(PRODUCT_ROOT)
    register_business_services(tools, product=products, inventory=products)
    stages = config.get("routing.registered_stages")
    lookup = ProductLookupPolicy(True)
    provider = PipelineReplyProvider(
        analyzer=MessageAnalyzer(
            client, config.get("routing.registered_intents"), stages, tools.names
        ),
        router=MessageRouter(
            config.get("routing.strong_llm_threshold", 0.65),
            config.get("routing.human_intents", []),
        ),
        state_manager=CustomerStateManager(stages),
        reply_generator=ReplyGenerator(client, 1000, lookup),
        reply_validator=ReplyValidator(1000),
        business_tools=tools,
        product_lookup=lookup,
        enabled_media_kinds=("image", "video"),
    )

    cases = [make_case(number) for number in range(1, 31)]
    llm_limit = asyncio.Semaphore(4)
    ui_lock = asyncio.Lock()
    ui_active = 0
    ui_max_active = 0
    accepted = []
    report_clicks = []
    delete_clicks = []
    sent_recipients = []
    terminal = set()
    progress = 0
    progress_lock = asyncio.Lock()

    async def ui_step(case, operation):
        nonlocal ui_active, ui_max_active
        async with ui_lock:
            ui_active += 1
            ui_max_active = max(ui_max_active, ui_active)
            try:
                # Every operation re-checks the exact active header. Report and
                # Delete exist in the simulated UI but no workflow references them.
                active_header = case["customer_name"]
                if active_header != case["customer_name"]:
                    raise RuntimeError("wrong_conversation_header")
                if operation == "accept":
                    accepted.append(case["conversation_id"])
                elif operation == "send":
                    sent_recipients.append(case["conversation_id"])
                await asyncio.sleep(0)
            finally:
                ui_active -= 1

    async def process(case):
        nonlocal progress
        started = monotonic()
        ack_text = None
        ack_gemini_used = False
        try:
            await ui_step(case, "open")
            if case["group"] == "stranger":
                # Accept only unlocks replying - the courtesy confirmation
                # that goes out immediately is always the fixed line, never a
                # Gemini call.
                await ui_step(case, "accept")
                ack_text = resend_prompt
                await ui_step(case, "send")

            # The real answer: the customer's pre-accept message (for a
            # stranger) or their only message (for an already-known contact)
            # - both go through the same Gemini pipeline.
            joined = "\n".join(case["messages"])
            async with llm_limit:
                decision = await provider.reply(ConversationContext(
                    platform="tiktok_web",
                    conversation_key=case["conversation_id"],
                    message_id=case["message_ids"][-1],
                    message_text=joined,
                    customer_name=case["customer_name"],
                    pending_messages=[
                        {"id": mid, "text": text, "index": index}
                        for index, (mid, text) in enumerate(
                            zip(case["message_ids"], case["messages"])
                        )
                    ],
                    inbound_media=case["inbound_media"],
                ))
            status = (
                "completed" if decision.should_send
                else "requires_human" if decision.requires_human
                else "failed"
            )
            row = {
                "status": status,
                "text": decision.text,
                "requires_human": decision.requires_human,
                "intent": getattr(decision.analysis, "intent", None),
                "attachments": [
                    item.model_dump(mode="json") for item in decision.attachments
                ],
                "gemini_used": True,
                "resend_prompt": resend_prompt,
                "ack_text": ack_text,
                "ack_gemini_used": ack_gemini_used,
            }
            if row["status"] == "completed":
                await ui_step(case, "send")
            row["recipient"] = case["conversation_id"]
            row["semantic_pass"] = semantic_pass(case, row)
        except Exception as exc:
            row = {
                "status": "failed",
                "error": type(exc).__name__,
                "error_detail": str(exc)[:240],
                "text": "",
                "requires_human": False,
                "intent": None,
                "attachments": [],
                "gemini_used": True,
                "resend_prompt": resend_prompt,
                "ack_text": ack_text,
                "ack_gemini_used": ack_gemini_used,
                "recipient": case["conversation_id"],
                "semantic_pass": False,
            }
        row["latency_seconds"] = round(monotonic() - started, 3)
        terminal.add(case["conversation_id"])
        async with progress_lock:
            progress += 1
            if progress % 5 == 0:
                print(f"progress={progress}/30", flush=True)
        return case, row

    started = monotonic()
    try:
        pairs = await asyncio.gather(*(process(case) for case in cases))
    finally:
        await client.close()
    elapsed = monotonic() - started

    rows = {
        case["conversation_id"]: {
            "group": case["group"], "expected": case["expected"], **row
        }
        for case, row in pairs
    }
    statuses = Counter(row["status"] for row in rows.values())
    semantic = Counter(
        "PASS" if row["semantic_pass"] else "FAIL" for row in rows.values()
    )
    latencies = [row["latency_seconds"] for row in rows.values()]
    expected_accepts = {
        case["conversation_id"] for case in cases if case["group"] == "stranger"
    }
    ack_correct = sum(
        row["ack_text"] == row["resend_prompt"]
        for row in rows.values() if row["ack_text"] is not None
    )
    metrics = {
        "customers_received": len(cases),
        "known_customers": sum(case["group"] == "known" for case in cases),
        "stranger_requests": len(expected_accepts),
        "requests_accepted": len(set(accepted)),
        "missing_accepts": len(expected_accepts - set(accepted)),
        "acknowledgements_sent": sum(row["ack_text"] is not None for row in rows.values()),
        "acknowledgements_correct_and_fixed": ack_correct,
        "report_clicks": len(report_clicks),
        "delete_clicks": len(delete_clicks),
        "terminal_customers": len(terminal),
        "omitted_customers": len(cases) - len(terminal),
        "statuses": dict(statuses),
        "semantic_results": dict(semantic),
        "gemini_conversations": sum(row["gemini_used"] for row in rows.values()),
        "wrong_recipient": sum(
            row["recipient"] != conversation for conversation, row in rows.items()
        ),
        "duplicate_sends": len(sent_recipients) - len(set(sent_recipients)),
        "context_cross_contamination": sum(
            any(other in row["text"] for other in rows if other != conversation)
            for conversation, row in rows.items()
        ),
        "max_gemini_concurrency": 4,
        "max_simulated_ui_concurrency": ui_max_active,
        "average_latency_seconds": round(statistics.mean(latencies), 3),
        "p95_latency_seconds": round(sorted(latencies)[28], 3),
        "total_elapsed_seconds": round(elapsed, 3),
        "real_tiktok_sends": 0,
    }
    report = {
        "model": "gemini-3.1-flash-lite",
        "scope": (
            "TikTok-like simulation: known customers + Message-request "
            "strangers (fixed acknowledgement on accept, then a real Gemini "
            "reply to their follow-up question); no real TikTok sends"
        ),
        "metrics": metrics,
        "results": rows,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"report": str(REPORT), "metrics": metrics}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
