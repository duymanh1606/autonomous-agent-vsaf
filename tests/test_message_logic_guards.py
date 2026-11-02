import unittest
from datetime import datetime, timezone

from agent.message_analyzer import MessageAnalyzer
from agent.reply_validator import ReplyValidator
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.reply import ReplyResult
from models.tool import ToolResult


class StaticAnalysisLlm:
    model_name = "test"

    def __init__(self, analysis):
        self.analysis = analysis

    async def generate_structured(self, _prompt, _schema):
        return self.analysis.model_copy(deep=True)


def analysis(**overrides):
    values = {
        "intent": "ask_price",
        "next_stage": "asking_price",
        "can_reply_directly": True,
        "confidence": 0.95,
    }
    values.update(overrides)
    return MessageAnalysis(**values)


class GreetingMisclassificationGuardTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces a real production defect: Gemini classified clear
    purchase-intent messages ("Tôi muốn mua giày bóng đá", "Bạn có những
    loại nào") as "greeting", which then got a generic canned reply -
    sometimes even in the wrong language - instead of a real answer."""

    async def test_purchase_intent_misclassified_as_greeting_is_corrected(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="greeting", next_stage="new")),
            registered_intents=("greeting", "ask_product", "unknown"),
            registered_stages=("new", "asking_product", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-1", message="Tôi muốn mua giày bóng đá",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.intent, "ask_product")
        self.assertEqual(result.next_stage, "asking_product")

    async def test_a_real_greeting_is_left_alone(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="greeting", next_stage="new")),
            registered_intents=("greeting", "ask_product", "unknown"),
            registered_stages=("new", "asking_product", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-2", message="Chào shop",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.intent, "greeting")
        self.assertEqual(result.next_stage, "new")

    async def test_product_types_question_misclassified_as_greeting_is_corrected(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="greeting", next_stage="new")),
            registered_intents=("greeting", "ask_product", "unknown"),
            registered_stages=("new", "asking_product", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-3", message="Bạn có những loại nào",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.intent, "ask_product")


class PromotionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_only_inbound_cannot_request_catalogue_media_or_tool(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="ask_product",
                requested_media=["image", "video"],
                requires_tool=True,
                tool_name="get_product",
                tool_args={"product_id": "sp006"},
            )),
            registered_intents=("ask_product", "unknown"),
            registered_stages=("asking_price", "human_required"),
            available_tools=("get_product",),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="media-1", message="",
            metadata={"media": [{"kind": "image", "src": "blob:customer-image"}]},
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.requested_media, [])
        self.assertFalse(result.requires_tool)
        self.assertIsNone(result.tool_name)

    def test_media_request_is_derived_only_from_explicit_current_text(self):
        cases = (
            ("Shop gửi mình cả ảnh và video Venus BigOne nhé", ["image", "video"]),
            ("Cho mình xem hình sữa Milo", ["image"]),
            ("Shop ơi, có Milo không? Gửi ảnh cho mình nhé", ["image"]),
            ("Bạn có thể gửi mình video không?", ["video"]),
            ("Có minh hoạ không", ["image"]),
            ("Đâu hình ảnh đâu", ["image"]),
            ("Không thấy ảnh sản phẩm đâu", ["image"]),
            ("Tôi vừa gửi ảnh sản phẩm", []),
            ("Shop đã gửi ảnh rồi", []),
            ("Không cần gửi ảnh nữa", []),
            ("", []),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    MessageAnalyzer._explicit_media_requests(text), expected
                )

    def test_burmese_media_request_is_recognised(self):
        """A `reply_language: my` shop was never sending catalogue media: every
        request pattern was Vietnamese, and Burmese writes a request as one
        unspaced run of characters that no \\b-anchored pattern can match."""
        cases = (
            # "Please send a photo" / "send me a photo of this product".
            ("ဓာတ်ပုံပို့ပေးပါ", ["image"]),
            ("ဒီပစ္စည်းရဲ့ ဓာတ်ပုံ ပို့ပေးပါ", ["image"]),
            # "I want to see a video" / "send a photo and a video".
            ("ဗီဒီယို ကြည့်ချင်ပါတယ်", ["video"]),
            ("ဓာတ်ပုံနဲ့ ဗီဒီယို ပို့ပေးပါ", ["image", "video"]),
            # "Where is the photo?" / "I did not get the photo."
            ("ဓာတ်ပုံ ဘယ်မှာလဲ", ["image"]),
            ("ဓာတ်ပုံ မရဘူး", ["image"]),
            # "Don't send photos" / "I don't need a video".
            ("ဓာတ်ပုံ မပို့ပါနဲ့", []),
            ("ဗီဒီယို မလိုပါဘူး", []),
            # "I already sent the photo" - inbound media, not a request.
            ("ဓာတ်ပုံ ပို့လိုက်ပါတယ်", []),
            # A photo mentioned with nothing asked of us.
            ("ဓာတ်ပုံ လှပါတယ်", []),
            # Burmese text with a Latin loanword still reads as a request.
            ("video ပို့ပေးပါ", ["video"]),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    MessageAnalyzer._explicit_media_requests(text), expected
                )

    def test_vietnamese_refusal_is_not_overturned_by_the_burmese_reader(self):
        """Both readers see every message, so a Vietnamese "no" must stay no:
        "photo" is a word the Burmese reader knows."""
        for text in ("Không cần gửi photo nữa", "Đừng gửi video cho tôi"):
            with self.subTest(text=text):
                self.assertEqual(MessageAnalyzer._explicit_media_requests(text), [])

    async def test_burmese_refund_image_request_remains_human_owned(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="complaint", next_stage="human_required",
                requires_human=True,
            )),
            registered_intents=("ask_product", "complaint", "unknown"),
            registered_stages=("asking_product", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-my-refund",
            # "Send me a photo of the refund."
            message="ပြန်အမ်းတဲ့ ဓာတ်ပုံ ပို့ပေးပါ",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.requested_media, ["image"])
        self.assertEqual(result.intent, "complaint")
        self.assertTrue(result.requires_human)

    async def test_safe_image_followup_is_not_silenced_as_a_complaint(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="complaint", next_stage="human_required",
                requires_human=True,
            )),
            registered_intents=("ask_product", "complaint", "unknown"),
            registered_stages=("asking_product", "human_required"),
            available_tools=("get_product",),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-image", message="Đâu hình ảnh đâu",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.requested_media, ["image"])
        self.assertEqual(result.intent, "ask_product")
        self.assertFalse(result.requires_human)

    async def test_sensitive_image_request_remains_human_owned(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="complaint", next_stage="human_required",
                requires_human=True,
            )),
            registered_intents=("ask_product", "complaint", "unknown"),
            registered_stages=("asking_product", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-refund", message="Gửi ảnh hóa đơn hoàn tiền cho tôi",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.requested_media, ["image"])
        self.assertEqual(result.intent, "complaint")
        self.assertTrue(result.requires_human)

    async def test_discount_question_is_handed_off_even_if_model_calls_it_price(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis()),
            registered_intents=("ask_price", "unknown"),
            registered_stages=("asking_price", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m1", message="Mua 10 hộp có được giảm 20% không?",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertTrue(result.requires_human)
        self.assertEqual(result.next_stage, "human_required")
        self.assertFalse(result.requires_tool)

    async def test_explicit_contact_facts_survive_when_model_omits_them(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="ask_order", next_stage="ordering")),
            registered_intents=("ask_order", "unknown"),
            registered_stages=("ordering", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m1",
            message=(
                "Mình tên Nguyễn Văn A, số điện thoại 0912345678, "
                "nhận hàng tại số 10 đường ABC, Hà Nội."
            ),
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.customer_facts, {
            "name": "Nguyễn Văn A",
            "phone": "0912345678",
            "address": "số 10 đường ABC, Hà Nội",
        })
        self.assertEqual(result.explicit_customer_facts, result.customer_facts)

    async def test_email_city_and_zalo_are_normalized_from_the_new_message(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="ask_order", next_stage="ordering")),
            registered_intents=("ask_order", "unknown"),
            registered_stages=("ordering", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m2",
            message=(
                "Địa chỉ: 25 Nguyễn Trãi, Quận 1; "
                "email: KHACH.HANG+TIKTOK@example.com; "
                "Zalo: +84 912 345 678; thành phố: Hồ Chí Minh"
            ),
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.explicit_customer_facts, {
            "phone": "+84912345678",
            "email": "khach.hang+tiktok@example.com",
            "address": "25 Nguyễn Trãi, Quận 1",
            "city": "Hồ Chí Minh",
        })

    async def test_bare_location_reply_is_captured_as_a_trimmed_address(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="ask_order", next_stage="ordering")),
            registered_intents=("ask_order", "unknown"),
            registered_stages=("ordering", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-bare-address",
            message="Mình ở cổ nhuế",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.customer_facts.get("address"), "cổ nhuế")
        self.assertEqual(result.explicit_customer_facts, {"address": "cổ nhuế"})

    async def test_model_fact_not_present_in_the_new_message_is_dropped(self):
        """Reproduces the dashboard bug: the model echoed the shop's own
        "send your address" outbound line as if it were the customer's
        address, because it was shown as context but never said by the
        customer in this message."""
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="ask_order", next_stage="ordering",
                customer_facts={
                    "address": (
                        "nhận hàng và số điện thoại liên lạc để shop tiến "
                        "hành hỗ trợ bạn lên đơn hàng nhé ạ"
                    ),
                    "phone": "012345678",
                },
            )),
            registered_intents=("ask_order", "unknown"),
            registered_stages=("ordering", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-phone-only",
            message="012345678",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertNotIn("address", result.customer_facts)
        self.assertEqual(result.customer_facts.get("phone"), "012345678")

    async def test_address_change_request_without_value_is_not_stored_as_address(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="complaint", next_stage="human_required",
                customer_facts={"address": "với sdt"},
            )),
            registered_intents=("complaint", "unknown"),
            registered_stages=("human_required",),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-address-change",
            message="Tôi muốn đổi địa chỉ với sdt",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertNotIn("address", result.customer_facts)
        self.assertEqual(result.explicit_customer_facts, {})

    def test_address_change_with_replacement_value_extracts_only_the_value(self):
        self.assertEqual(
            MessageAnalyzer._explicit_customer_facts(
                "Tôi muốn đổi địa chỉ sang Từ Liêm"
            ),
            {"address": "Từ Liêm"},
        )

    async def test_multi_item_order_is_not_partially_answered(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(intent="ask_product", next_stage="ordering")),
            registered_intents=("ask_product", "unknown"),
            registered_stages=("ordering", "human_required"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m1",
            message="Mình muốn mua 6 hộp Milo và 2 hộp Sữa Ông Thọ",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertTrue(result.requires_human)
        self.assertEqual(result.next_stage, "human_required")


class KnownAddressReplyGuardTests(unittest.TestCase):
    def test_reply_cannot_claim_or_print_an_image_when_no_attachment_exists(self):
        item = analysis(intent="ask_product", next_stage="asking_price")
        for text in (
            "Đây là hình ảnh sản phẩm ạ.",
            "Bạn xem tại [images/front.jpg] nhé.",
        ):
            with self.subTest(text=text):
                result = ReplyValidator().validate(
                    ReplyResult(text=text, should_send=True),
                    item,
                    tool_result=None,
                    last_outbound_text=None,
                    attachments=(),
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, "claims_missing_attachment")


class PriceStockRoutingGuardTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces a real production defect: a message asking price and stock
    together routed to check_inventory, which carries no price field, and the
    model invented one anyway. get_product carries both, so a combined
    question must use it instead."""

    async def test_combined_price_and_stock_question_uses_get_product(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="check_inventory", next_stage="checking_inventory",
                requires_tool=True, tool_name="check_inventory",
                tool_args={"product_id": "kem A1", "size": "50ml"},
            )),
            registered_intents=("check_inventory", "ask_price", "unknown"),
            registered_stages=("checking_inventory", "human_required"),
            available_tools=("check_inventory", "get_product"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-1", message="Kem A1 giá nhiêu, còn size 50ml không shop?",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.tool_name, "get_product")
        self.assertEqual(result.tool_args, {"product_id": "kem A1"})

    async def test_stock_only_question_still_uses_check_inventory(self):
        analyzer = MessageAnalyzer(
            StaticAnalysisLlm(analysis(
                intent="check_inventory", next_stage="checking_inventory",
                requires_tool=True, tool_name="check_inventory",
                tool_args={"product_id": "kem A1", "size": "50ml"},
            )),
            registered_intents=("check_inventory", "ask_price", "unknown"),
            registered_stages=("checking_inventory", "human_required"),
            available_tools=("check_inventory", "get_product"),
        )
        event = MessageEvent(
            platform="tiktok_web", conversation_id="c1", customer_id="u1",
            message_id="m-2", message="Kem A1 còn size 50ml không shop?",
            timestamp=datetime.now(timezone.utc),
        )

        result = await analyzer.analyze(
            event, CustomerState(customer_id="u1", conversation_id="c1"), []
        )

        self.assertEqual(result.tool_name, "check_inventory")


class UnverifiedPriceClaimGuardTests(unittest.TestCase):
    """Fail-closed backstop for the same defect: even if a price is invented
    despite the correct tool being called (or despite the prompt's
    instruction not to), the validator must never let it through."""

    def test_price_not_present_in_tool_result_is_rejected(self):
        item = analysis(
            intent="check_inventory", next_stage="checking_inventory",
            requires_tool=True, tool_name="check_inventory",
        )
        result = ReplyValidator().validate(
            ReplyResult(
                text="Dạ sản phẩm còn hàng, giá 350.000 đồng ạ.",
                should_send=True,
            ),
            item,
            tool_result=ToolResult(
                tool_name="check_inventory", success=True,
                data={"product_id": "sp1", "stock": 27, "available": True},
            ),
            last_outbound_text=None,
            attachments=(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "unverified_price_claim")

    def test_price_matching_tool_result_is_allowed(self):
        item = analysis(
            intent="ask_price", next_stage="asking_price",
            requires_tool=True, tool_name="get_product",
        )
        result = ReplyValidator().validate(
            ReplyResult(
                text="Dạ sản phẩm giá 259.000 đồng ạ.",
                should_send=True,
            ),
            item,
            tool_result=ToolResult(
                tool_name="get_product", success=True,
                data={"id": "sp1", "price": 259000, "stock": 42},
            ),
            last_outbound_text=None,
            attachments=(),
        )
        self.assertTrue(result.allowed)

    def test_an_invented_burmese_price_is_rejected_too(self):
        """A Myanmar shop (messaging.reply_language: my) gets the same
        backstop: the amount is written in Myanmar digits and kyat, so the
        guard has to read both to see it was never verified."""
        item = analysis(
            intent="check_inventory", next_stage="checking_inventory",
            requires_tool=True, tool_name="check_inventory",
        )
        result = ReplyValidator().validate(
            ReplyResult(text="ပစ္စည်း ရှိပါတယ်၊ ၃၅၀၀၀ ကျပ် ပါရှင်", should_send=True),
            item,
            tool_result=ToolResult(
                tool_name="check_inventory", success=True,
                data={"product_id": "sp1", "stock": 27, "available": True},
            ),
            last_outbound_text=None,
            attachments=(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "unverified_price_claim")

    def test_a_burmese_price_matching_the_tool_result_is_allowed(self):
        item = analysis(
            intent="ask_price", next_stage="asking_price",
            requires_tool=True, tool_name="get_product",
        )
        result = ReplyValidator().validate(
            ReplyResult(text="ဒီပစ္စည်း 25,000 Ks ပါရှင်", should_send=True),
            item,
            tool_result=ToolResult(
                tool_name="get_product", success=True,
                data={"id": "sp1", "price": 25000, "stock": 42},
            ),
            last_outbound_text=None,
            attachments=(),
        )
        self.assertTrue(result.allowed)

    def test_reply_with_no_price_mention_is_unaffected(self):
        item = analysis(
            intent="ask_shipping", next_stage="after_sales",
            requires_tool=False,
        )
        result = ReplyValidator().validate(
            ReplyResult(text="Dạ đơn hàng giao trong 3-5 ngày ạ.", should_send=True),
            item,
            tool_result=None,
            last_outbound_text=None,
            attachments=(),
        )
        self.assertTrue(result.allowed)

    def test_image_claim_is_allowed_with_a_verified_attachment(self):
        item = analysis(intent="ask_product", next_stage="asking_price")
        result = ReplyValidator().validate(
            ReplyResult(text="Đây là hình ảnh sản phẩm ạ.", should_send=True),
            item,
            tool_result=None,
            last_outbound_text=None,
            attachments=(object(),),
        )
        self.assertTrue(result.allowed)

    def test_fabricated_media_url_is_blocked_even_with_a_verified_attachment(self):
        """Reproduces a real production reply: the model was told not to write
        a local file path, so it invented a public-looking URL for the same
        image instead. A real attachment being sent alongside must not make
        that fabricated link acceptable - TikTok DM has no inline-link
        mechanism, so any such string in the text is always wrong."""
        item = analysis(intent="ask_product", next_stage="asking_price")
        text = (
            "Dạ, em xin gửi anh/chị hình ảnh sản phẩm Gà Rán Jollibee để mình tham "
            "khảo ạ. Anh/chị xem hình ảnh sản phẩm tại đây nhé: "
            "https://cdn.example.com/images/f3a43eedabdb41fd.png. Anh/chị cần em "
            "hỗ trợ thêm thông tin gì về món gà rán này không ạ?"
        )
        result = ReplyValidator().validate(
            ReplyResult(text=text, should_send=True),
            item,
            tool_result=None,
            last_outbound_text=None,
            attachments=(object(),),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "claims_missing_attachment")

    def test_fabricated_media_url_without_attachment_is_also_blocked(self):
        item = analysis(intent="ask_product", next_stage="asking_price")
        result = ReplyValidator().validate(
            ReplyResult(
                text="Ảnh sản phẩm đây ạ: https://cdn.example.com/images/abc123.png",
                should_send=True,
            ),
            item,
            tool_result=None,
            last_outbound_text=None,
            attachments=(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "claims_missing_attachment")

    def test_reply_cannot_ask_for_address_already_present_in_history(self):
        item = analysis(intent="ask_shipping", next_stage="ordering")
        item.verified_customer_context = {
            "recent_messages": [{
                "direction": "INBOUND",
                "text": "Nhận hàng tại số 10 đường ABC, Hà Nội",
            }]
        }

        result = ReplyValidator().validate(
            ReplyResult(
                text="Bạn vui lòng cung cấp địa chỉ giao hàng giúp shop nhé.",
                should_send=True,
            ),
            item,
            tool_result=None,
            last_outbound_text=None,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "asks_for_known_address")

    def test_address_question_is_allowed_when_no_address_is_known(self):
        item = analysis(intent="ask_shipping", next_stage="ordering")

        result = ReplyValidator().validate(
            ReplyResult(
                text="Bạn vui lòng cung cấp địa chỉ giao hàng giúp shop nhé.",
                should_send=True,
            ),
            item,
            tool_result=None,
            last_outbound_text=None,
        )

        self.assertTrue(result.allowed)
