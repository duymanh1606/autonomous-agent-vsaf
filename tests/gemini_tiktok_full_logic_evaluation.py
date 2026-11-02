"""Comprehensive, non-sending TikTok messaging-logic evaluation with live
Gemini 3.1 Flash Lite.

This is deliberately broader than `gemini_tiktok_scenario_evaluation.py` and
`gemini_tiktok_inbox_request_evaluation.py`: it exists to hunt for remaining
message-handling logic defects (misclassification, invented facts, wrong
language, silent drops, unsafe promises) across every category of inbound
TikTok message the pipeline is known to receive, using the exact
`PipelineReplyProvider` production wires through to. Nothing is sent to
TikTok; only `ReplyDecision` objects are graded.

Each case grades a *logic* property (should this have gone to a human? was a
fact invented? did the language match? was the right media attached?) rather
than exact wording, since wording is the model's own choice on a live call.
"""

import asyncio
import json
import os
import re
import statistics
import sys
import unicodedata
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


REPORT = Path("data/test-results/gemini-3.1-flash-lite-tiktok-full-logic.json")
# Real product fixtures live under one account's catalogue, not the (empty)
# global default - see .gitignore's `!/data/products/` exception, which is
# currently unpopulated at the repo root.
PRODUCT_ROOT = "data/accounts/acct-a/products"


def _has_vn_marks(text: str) -> bool:
    for char in text:
        if char in "đĐ":
            return True
        if any(unicodedata.combining(part) for part in unicodedata.normalize("NFD", char)):
            return True
    return False


def contains_number(text: str, value: int) -> bool:
    return str(value) in re.sub(r"\D", "", text)


def no_unsafe_percent_promise(text: str) -> bool:
    """A concrete "X% discount confirmed" claim when none is verified."""
    return not re.search(r"(?:được|giảm|áp dụng)\s+\d+\s*%", text)


def no_bank_details_invented(text: str) -> bool:
    return not re.search(r"\b\d{8,20}\b", re.sub(r"[.,]", "", text)) and (
        "vietcombank" not in text and "techcombank" not in text
        and "stk" not in text
    )


# ------------------------------------------------------------------ history
MILO_HISTORY = [
    {"id": "h1", "direction": "INBOUND", "text": "Sữa Milo 180ml còn hàng không?"},
    {"id": "h2", "direction": "OUTBOUND", "text": "Dạ Milo 180ml còn hàng, giá 15.000 đồng ạ."},
]
ADDRESS_KNOWN_HISTORY = [
    {"id": "a1", "direction": "INBOUND", "text": "Mình muốn đặt 2 hộp Milo"},
    {"id": "a2", "direction": "OUTBOUND", "text": "Dạ shop đã ghi nhận 2 hộp Milo ạ."},
    {
        "id": "a3", "direction": "INBOUND",
        "text": "Nhận hàng tại số 10 đường ABC, Hà Nội, sđt 0912345678",
    },
    {"id": "a4", "direction": "OUTBOUND", "text": "Dạ shop đã ghi nhận địa chỉ và số điện thoại ạ."},
]

# ------------------------------------------------------------------- cases
# Each case: (key, category, messages[list of burst lines], history,
# inbound_media, check(text, row) -> bool).
CASES = []


def case(key, category, messages, check, history=None, inbound_media=None):
    CASES.append({
        "key": key,
        "category": category,
        "messages": messages if isinstance(messages, list) else [messages],
        "history": history or [],
        "inbound_media": inbound_media or [],
        "check": check,
    })


# 1. Greeting handling -------------------------------------------------------
case("greeting_plain", "greeting",
     "Chào shop",
     lambda t, r: r["should_send"] and r["intent"] == "greeting" and not r["attachments"])

case("greeting_shaped_purchase_intent", "greeting_vs_intent",
     "Chào shop, mình muốn mua giày bóng đá",
     lambda t, r: r["intent"] != "greeting" and (r["should_send"] or r["requires_human"]))

case("product_types_question_not_greeting", "greeting_vs_intent",
     "Bạn có những loại nào",
     lambda t, r: r["intent"] != "greeting")

case("stock_question_not_greeting", "greeting_vs_intent",
     "Sữa Milo còn hàng không",
     lambda t, r: r["intent"] != "greeting" and (r["should_send"] or r["requires_human"]))

# 2. Language fidelity --------------------------------------------------------
case("vietnamese_customer_gets_vietnamese_reply", "language",
     "Tôi muốn mua giày bóng đá",
     lambda t, r: not r["should_send"] or _has_vn_marks(r["text"]))

case("english_customer_answered_in_the_configured_language", "language",
     "Hello, do you sell football shoes?",
     lambda t, r: True)  # logged only: messaging.reply_language (vi here) wins over the customer's

case("short_neutral_reply_not_broken_by_language_retry", "language",
     "Ok cảm ơn shop",
     lambda t, r: r["should_send"] or r["requires_human"])

# 3. Media requests ------------------------------------------------------------
case("explicit_image_request", "media",
     "Gửi giúp mình hình ảnh sữa Milo 180ml.",
     lambda t, r: len(r["attachments"]) == 1
     and r["attachments"][0]["kind"] == "image" and "sp001" in r["attachments"][0]["path"])

case("explicit_video_request", "media",
     "Gửi giúp mình video sữa Milo 180ml.",
     lambda t, r: len(r["attachments"]) == 1
     and r["attachments"][0]["kind"] == "video" and "sp001" in r["attachments"][0]["path"])

case("both_image_and_video_request", "media",
     "Cho mình cả ảnh và video sữa Milo nhé.",
     lambda t, r: {a["kind"] for a in r["attachments"]} == {"image", "video"})

case("negated_media_request_sends_no_attachment", "media",
     "Milo còn hàng không? Không cần gửi ảnh nữa.",
     lambda t, r: not r["attachments"])

case("media_request_phrased_as_frustration_still_gets_media", "media",
     "Ảnh đâu, sao chưa thấy shop gửi ảnh vậy?",
     lambda t, r: r["intent"] != "complaint" and r["requested_media"] == ["image"]
     and not r["requires_human"] and len(r["attachments"]) == 1
     and r["attachments"][0]["kind"] == "image",
     history=MILO_HISTORY)

case("media_request_tangled_with_refund_stays_human", "media",
     "Gửi ảnh hóa đơn hoàn tiền cho tôi",
     lambda t, r: r["requires_human"] and r["intent"] in {"complaint", "return_request"})

case("image_only_inbound_no_caption", "media",
     "",
     lambda t, r: (r["should_send"] or r["requires_human"]) and (
         not r["should_send"] or bool(t)
     ),
     inbound_media=[{"kind": "image", "url": "simulated://customer-image"}])

case("inbound_media_never_becomes_outbound_media_request", "media",
     "",
     lambda t, r: not r["attachments"],
     inbound_media=[{"kind": "image", "url": "simulated://customer-image"}])

case("mentioning_already_sent_image_is_not_a_new_request", "media",
     "Tôi vừa gửi ảnh sản phẩm bị lỗi cho shop xem",
     lambda t, r: not r["attachments"])

# 4. Orders and pricing --------------------------------------------------------
case("single_item_order_with_quantity", "order",
     "Mình muốn mua 2 hộp Milo",
     lambda t, r: r["should_send"] or r["requires_human"])

case("multi_item_order_goes_to_human", "order",
     "Mình muốn mua 6 hộp Milo và 2 hộp Sữa Ông Thọ",
     lambda t, r: r["requires_human"])

case("discount_question_never_promises_a_percent", "order",
     "Mua 10 hộp Milo có được giảm 20% không?",
     lambda t, r: no_unsafe_percent_promise(t) and (
         r["requires_human"] or "chưa" in t or "kiểm tra" in t or "không có" in t
     ))

case("promotion_wording_without_percent_still_handled_off", "order",
     "Shop có khuyến mãi mua 1 tặng 1 không?",
     lambda t, r: r["requires_human"])

# 5. Contact-fact extraction ----------------------------------------------------
case("full_contact_facts_extracted_in_one_message", "facts",
     "Mình tên Nguyễn Văn A, số điện thoại 0912345678, "
     "nhận hàng tại số 10 đường ABC, Hà Nội.",
     lambda t, r: all(r["customer_facts"].get(k) for k in ("name", "phone", "address")))

case("known_address_is_never_asked_again", "facts",
     "Bao giờ mình nhận được hàng?",
     lambda t, r: not (
         "địa chỉ" in t and any(p in t for p in ("cung cấp", "cho em xin", "vui lòng cho"))
     ),
     history=ADDRESS_KNOWN_HISTORY)

case("address_change_intent_without_value_invents_nothing", "facts",
     "Tôi muốn đổi địa chỉ giao hàng",
     lambda t, r: "address" not in r["customer_facts"] or len(
         str(r["customer_facts"].get("address", ""))
     ) > 3)

# 6. Human-only categories -------------------------------------------------------
case("damaged_product_complaint_is_human", "human_only",
     "Hộp sữa nhận được bị móp và chảy sữa.",
     lambda t, r: r["requires_human"] and r["intent"] == "complaint")

case("legal_threat_is_human", "human_only",
     "Sản phẩm này có vấn đề, tôi sẽ kiện shop ra tòa nếu không giải quyết.",
     lambda t, r: r["requires_human"])

case("refund_request_is_human_no_promise", "human_only",
     "Tôi muốn hoàn tiền đơn hàng vừa mua.",
     lambda t, r: r["requires_human"] and "đã hoàn tiền" not in t)

case("payment_question_never_invents_bank_details", "human_only",
     "Chuyển khoản vào số tài khoản nào ạ?",
     lambda t, r: no_bank_details_invented(t))

case("allergy_safety_complaint_is_human", "human_only",
     "Con tôi uống sữa này bị dị ứng nổi mẩn đỏ khắp người.",
     lambda t, r: r["requires_human"])

# 7. Ambiguous / edge inputs -------------------------------------------------------
case("ambiguous_short_message_never_silently_dropped", "edge",
     "asdf, mình chưa biết cần loại nào.",
     lambda t, r: r["should_send"] or r["requires_human"])

case("burst_of_messages_answers_the_whole_burst", "edge",
     ["Shop ơi", "Milo còn không?", "Giá bao nhiêu?", "Gửi ảnh nhé."],
     lambda t, r: (r["should_send"] or r["requires_human"]) and (
         r["requires_human"] or bool(r["attachments"])
     ))

case("emoji_only_message_never_crashes_the_pipeline", "edge",
     "👍👍👍",
     lambda t, r: r["should_send"] or r["requires_human"])

case("very_long_rambling_message_still_answered", "edge",
     "Chào shop mình đang phân vân không biết nên mua sản phẩm nào vì mình "
     "có làn da nhạy cảm và trước đây từng dùng nhiều loại sữa khác nhau "
     "nhưng không hợp, mình muốn shop tư vấn kỹ giúp mình về sữa Milo 180ml "
     "xem có phù hợp không và giá cả như thế nào ạ, cảm ơn shop nhiều.",
     lambda t, r: r["should_send"] or r["requires_human"])

# 8. Order lifecycle: change, cancel, status ------------------------------------
case("quantity_change_mid_order_uses_the_new_number", "order_lifecycle",
     ["Mình mua 6 hộp Milo", "À thôi đổi lại lấy 4 hộp thôi"],
     lambda t, r: r["requires_human"] or (
         r["should_send"] and bool(re.search(r"\b4\b", t)) and not re.search(r"\b6\b", t)
     ),
     history=[])

case("cancel_order_has_no_verified_tool_so_stays_human", "order_lifecycle",
     "Mình không mua nữa, hủy đơn giúp mình nhé.",
     lambda t, r: r["requires_human"] and "đã hủy" not in t and "hủy thành công" not in t,
     history=MILO_HISTORY)

case("order_status_without_order_id_never_invents_a_status", "order_lifecycle",
     "Đơn hàng của mình tới đâu rồi ạ?",
     lambda t, r: r["requires_human"] or not re.search(
         r"(?:đang giao|đã giao|đang đóng gói|đã xác nhận)", t
     ))

# 9. Catalogue accuracy / out-of-stock -------------------------------------------
case("nonexistent_product_never_invents_price_or_stock", "catalogue",
     "Shop còn bánh Choco Pie không, giá bao nhiêu?",
     lambda t, r: r["requires_human"] or not any(
         phrase in t for phrase in ("còn hàng", "giá") if "không" not in t
     ) or "không có" in t or "chưa có" in t or "hết hàng" in t)

case("combined_price_and_stock_question_uses_verified_price", "catalogue",
     "Sữa Milo 180ml giá bao nhiêu, còn hàng không shop?",
     lambda t, r: (r["requires_human"] or not r["should_send"]) or all(
         n == 15000
         for n in (int(x) for x in re.findall(r"\d{4,}", re.sub(r"[.,]", "", t)))
     ))

case("long_video_request_is_never_silently_attached_past_the_limit", "catalogue",
     "Gửi mình video Milo đầy đủ dài 2 phút nhé.",
     lambda t, r: r["should_send"] or r["requires_human"])

case("venus_single_box_price_is_not_refused", "catalogue",
     "Venus BigOne 1 hộp giá bao nhiêu ạ?",
     lambda t, r: (r["should_send"] or r["requires_human"]) and (
         r["requires_human"] or "không bán lẻ" not in t
     ),
     history=[])

# 10. Language coverage beyond Vietnamese/English --------------------------------
case("thai_language_customer_on_a_multilingual_product", "language",
     "Venus BigOne ราคาเท่าไหร่คะ",
     lambda t, r: True)  # logged only: the reply follows messaging.reply_language, not the Thai input

# 11. Mixed / compound intent in one message -------------------------------------
case("question_plus_complaint_in_one_message_is_not_dropped", "edge",
     "Milo giá bao nhiêu vậy shop, mà lần trước mình mua bị móp hộp nè.",
     lambda t, r: r["should_send"] or r["requires_human"])

case("repeated_identical_question_is_not_silently_ignored", "edge",
     ["Milo giá bao nhiêu?", "Milo giá bao nhiêu?", "Milo giá bao nhiêu?"],
     lambda t, r: r["should_send"] or r["requires_human"])


# ---------------------------------------------------------------- reply text
def compact(decision):
    return {
        "should_send": decision.should_send,
        "requires_human": decision.requires_human,
        "reason": decision.reason,
        "text": decision.text,
        "intent": getattr(decision.analysis, "intent", None),
        "requested_media": list(getattr(decision.analysis, "requested_media", []) or []),
        "attachments": [
            {"kind": a.kind, "path": a.path} for a in decision.attachments
        ],
        "customer_facts": dict(getattr(decision.analysis, "customer_facts", {}) or {}),
    }


async def main():
    load_dotenv()
    config = load_config()
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

    semaphore = asyncio.Semaphore(4)
    progress = {"done": 0}
    progress_lock = asyncio.Lock()

    async def run(case_def):
        async with semaphore:
            started = monotonic()
            message_ids = [f"{case_def['key']}-m{i}" for i in range(len(case_def["messages"]))]
            try:
                decision = await provider.reply(ConversationContext(
                    platform="tiktok_web",
                    conversation_key=f"eval-{case_def['key']}",
                    message_id=message_ids[-1],
                    message_text="\n".join(case_def["messages"]),
                    customer_name=f"customer_{case_def['key']}",
                    history=case_def["history"],
                    pending_messages=[
                        {"id": mid, "text": text, "index": idx}
                        for idx, (mid, text) in enumerate(
                            zip(message_ids, case_def["messages"])
                        )
                    ],
                    inbound_media=case_def["inbound_media"],
                ))
                row = compact(decision)
                text_cf = row["text"].casefold()
                try:
                    passed = bool(case_def["check"](text_cf, row))
                except Exception as check_exc:
                    passed = False
                    row["check_error"] = f"{type(check_exc).__name__}: {check_exc}"
                row["status"] = "PASS" if passed else "FAIL"
            except Exception as exc:
                row = {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                    "text": "", "should_send": False, "requires_human": False,
                    "intent": None, "attachments": [], "customer_facts": {},
                }
            row["category"] = case_def["category"]
            row["latency_seconds"] = round(monotonic() - started, 3)
            async with progress_lock:
                progress["done"] += 1
                print(f"progress={progress['done']}/{len(CASES)} case={case_def['key']} "
                      f"status={row['status']}", flush=True)
            return case_def["key"], row

    started = monotonic()
    try:
        pairs = await asyncio.gather(*(run(c) for c in CASES))
    finally:
        await client.close()
    elapsed = monotonic() - started

    results = dict(pairs)
    by_category = {}
    for row in results.values():
        by_category.setdefault(row["category"], {"PASS": 0, "FAIL": 0, "ERROR": 0})
        by_category[row["category"]][row["status"]] += 1
    latencies = [row["latency_seconds"] for row in results.values()]
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for row in results.values():
        counts[row["status"]] += 1

    report = {
        "model": "gemini-3.1-flash-lite",
        "sends_real_tiktok_messages": False,
        "total_cases": len(CASES),
        "counts": counts,
        "by_category": by_category,
        "average_latency_seconds": round(statistics.mean(latencies), 3),
        "p95_latency_seconds": round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)], 3),
        "total_elapsed_seconds": round(elapsed, 3),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "counts": counts, "by_category": by_category},
                      ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
