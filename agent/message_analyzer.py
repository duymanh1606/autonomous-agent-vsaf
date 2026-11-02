import json
import re

from agent.tool_policy import without_tool
from llm.base import LLMProvider
from models.analysis import MediaKind, MessageAnalysis
from models.conversation import ConversationMessage
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.message_errors import MessageErrorType, MessageProcessingError


TOOL_ARGUMENT_SCHEMAS = {
    "get_product": {"product_id": "product id or customer-visible product name"},
    "check_inventory": {
        "product_id": "product id or customer-visible product name",
        "size": "optional size exactly as requested by the customer",
    },
    "get_shipping": {"destination": "optional shipping destination"},
    "get_order": {"order_id": "order id", "customer_id": "customer id"},
    "get_customer": {"customer_id": "customer id"},
    "get_conversation_history": {
        "conversation_id": "conversation id",
        "limit": "optional integer limit",
    },
}


class MessageAnalyzer:
    def __init__(
        self, llm: LLMProvider, registered_intents, registered_stages,
        available_tools=(),
    ):
        self.llm = llm
        self.registered_intents = tuple(registered_intents)
        self.registered_stages = tuple(registered_stages)
        # The business tools the registry can actually execute. Without this
        # list the model invents plausible names (`get_product_price`), every
        # one of which fails at execution time and sends the customer to a
        # human instead of getting an answer.
        self.available_tools = tuple(available_tools)

    async def analyze(
        self,
        event: MessageEvent,
        state: CustomerState,
        recent_messages: list[ConversationMessage],
        customer_context: dict | None = None,
    ) -> MessageAnalysis:
        customer_context = customer_context or {}
        context = {
            "customer_state": state.model_dump(
                mode="json", exclude={"metadata", "last_message"}
            ),
            "conversation_summary": state.conversation_summary,
            "recent_messages": [message.model_dump(mode="json") for message in recent_messages],
            "customer_profile": customer_context.get("customer_profile", {}),
            "known_customer_facts": customer_context.get("customer_facts", {}),
            "new_message": event.message,
            "new_message_media": list((event.metadata or {}).get("media") or ()),
        }
        tool_schemas = {
            name: TOOL_ARGUMENT_SCHEMAS.get(name, {})
            for name in self.available_tools
        }
        prompt = (
            "Analyze one customer-service message. Return only the requested JSON schema. "
            "Never invent inventory, price, shipping, order, or customer data. "
            "Extract customer_facts only when explicitly stated in new_message; "
            "supported contact fields are name, phone, email, address and city. "
            "never infer sensitive or demographic information. Existing profile and facts "
            "are context only and must not be emitted as newly discovered facts. "
            "When new_message is empty but new_message_media is non-empty, acknowledge the "
            "received media and ask what help the customer wants; never guess the product "
            "identity from attachment metadata alone. "
            "Set requires_tool=true only when a fact needs a business system AND the "
            "operation is one of the registered tools below; tool_name must be copied "
            "from that list verbatim and tool_args must use exactly that tool's argument "
            "names. For get_product/check_inventory, put either a product id or the "
            "customer-visible product name in product_id; never emit product_name as a "
            "tool argument. Set requested_media to a JSON list: ['image'] when the customer "
            "explicitly asks for a product photo/image, ['video'] for an explicit video "
            "request, ['image', 'video'] when both are requested, otherwise []. A product "
            "media request must use "
            "get_product when the product is identifiable. Never put a file path in JSON. "
            "If no registered tool fits, set requires_tool=false. "
            "Set requires_human=true for sensitive, complaint, refund, legal, safety, "
            "payment or unclear repeated requests. "
            "A comment on the taste, colour or preparation of a product the customer "
            "already has is product_feedback, not complaint, and needs no human; "
            "complaint means a damaged, wrong or bad-quality item, an allergy or "
            "reaction, or an unresolved problem with an order. A question whether an "
            "order was sent or has arrived is order_status.\n"
            f"Registered intents: {json.dumps(self.registered_intents)}\n"
            f"Registered stages: {json.dumps(self.registered_stages)}\n"
            f"Registered tools: {json.dumps(self.available_tools)}\n"
            f"Tool argument schemas: {json.dumps(tool_schemas)}\n"
            f"Context: {json.dumps(context, ensure_ascii=False)}"
        )
        try:
            analysis = await self.llm.generate_structured(prompt, MessageAnalysis)
        except Exception as exc:
            raise MessageProcessingError(
                MessageErrorType.LLM_ERROR, "Message analysis failed"
            ) from exc
        if analysis.intent not in self.registered_intents:
            analysis.intent = "unknown"
            analysis.confidence = min(analysis.confidence, 0.49)
        if analysis.intent == "greeting" and self._asks_about_products(event.message):
            # Seen in production: "Tôi muốn mua giày bóng đá" and "Bạn có
            # những loại nào" both classified as "greeting", which then gets
            # a generic canned reply instead of a real answer. A message
            # carrying explicit purchase/product-inquiry language is never a
            # bare greeting, regardless of what the model decided.
            analysis.intent = (
                "ask_product" if "ask_product" in self.registered_intents
                else analysis.intent
            )
            analysis.next_stage = (
                "asking_product" if "asking_product" in self.registered_stages
                else analysis.next_stage
            )
        if analysis.next_stage not in self.registered_stages:
            analysis.next_stage = "human_required"
            analysis.requires_human = True
        if analysis.requires_tool and analysis.tool_name not in self.available_tools:
            # An unknown tool name is a naming mistake by the model, not a
            # reason to leave the customer unanswered: the request is dropped
            # and the message is answered on the ordinary LLM path.
            analysis = without_tool(analysis)
        if (
            analysis.requires_tool
            and analysis.tool_name == "check_inventory"
            and "get_product" in self.available_tools
            and self._asks_about_price(event.message)
        ):
            # check_inventory never returns a price field. Seen in production:
            # a message asking price *and* stock in one turn routed here, left
            # the reply generator with nothing verified to quote for the price
            # half, and it invented one. get_product carries both price and
            # total stock, so it is the tool that can actually answer this.
            analysis.tool_name = "get_product"
            analysis.tool_args = {
                "product_id": analysis.tool_args.get("product_id", "")
            }
        # Attachments are an outbound side effect, so the model is not the
        # authority for deciding whether the customer requested one. Derive
        # this only from the current inbound text; conversation history and an
        # inbound image must never turn into a new catalogue-media request.
        analysis.requested_media = self._explicit_media_requests(event.message)
        if (
            analysis.requested_media
            and analysis.intent in {"complaint", "unknown"}
            and self._safe_media_followup(event.message)
        ):
            # "Ảnh đâu?" is often classified as a complaint because the
            # customer is understandably unhappy. It is still a reversible,
            # catalogue-backed media request, not a case that must go silent
            # behind a human handoff. ProductLookupPolicy will require a
            # verified product before any file can be attached.
            analysis.intent = (
                "ask_product" if "ask_product" in self.registered_intents
                else analysis.intent
            )
            analysis.next_stage = (
                "asking_product" if "asking_product" in self.registered_stages
                else analysis.next_stage
            )
            analysis.requires_human = False
            analysis.can_reply_directly = False
        if not str(event.message or "").strip() and (event.metadata or {}).get("media"):
            analysis = without_tool(analysis)
        # Promotions and multi-line orders are not represented by any verified
        # business tool in this runtime. Gemini can classify "giảm 20%" as a
        # price question or answer only one item from a two-item order. Route
        # both deterministically to a person until those services exist.
        if self._asks_about_promotion(event.message) or self._is_multi_item_order(
            event.message
        ):
            analysis = without_tool(analysis)
            analysis.requires_human = True
            analysis.next_stage = "human_required"
        # Structured generation can still omit facts it plainly repeats in the
        # reply. Capture only labelled, explicit values from the new inbound
        # text; never infer demographics or mine old messages. Exact extraction
        # wins over a conflicting model value.
        explicit_facts = self._explicit_customer_facts(event.message)
        # The model is shown the whole recent thread (including the shop's own
        # outbound turns) as context, and can copy a value from there instead
        # of from new_message - e.g. echoing back the shop's own "send your
        # address" prompt as if it were the customer's address. A fact that
        # cannot be found in the actual inbound text is not trustworthy.
        analysis.customer_facts = self._facts_grounded_in_message(
            event.message, analysis.customer_facts, explicit_facts,
        )
        if (
            "address" not in explicit_facts
            and self._is_address_change_request(event.message)
        ):
            # The model may turn "tôi muốn đổi địa chỉ" into an address fact.
            # A request to update data is not the replacement value itself.
            analysis.customer_facts.pop("address", None)
        analysis.customer_facts = {
            **analysis.customer_facts,
            **explicit_facts,
        }
        analysis.set_explicit_customer_facts(explicit_facts)
        # This field is always supplied by verified persistence after model
        # validation; model-provided content is never trusted here.
        analysis.verified_customer_context = {
            "customer_profile": customer_context.get("customer_profile", {}),
            "customer_facts": customer_context.get("customer_facts", {}),
            # These messages came from the currently verified conversation or
            # its account-scoped store. The reply validator uses them only to
            # prevent asking for an address the customer already supplied.
            "recent_messages": [
                {
                    "direction": str(message.direction),
                    "text": message.text,
                }
                for message in recent_messages
            ],
        }
        return analysis

    @staticmethod
    def _asks_about_products(text: str) -> bool:
        """True when the message carries explicit purchase/product-inquiry
        language - "muốn mua", "có những loại nào", "còn hàng không", "giá
        bao nhiêu" - never just a hello. Deliberately narrow: only patterns
        seen misfiled as "greeting" in production are matched, so an actual
        bare greeting ("chào shop", "shop ơi") is never touched."""
        value = " ".join(str(text or "").casefold().split())
        if not value:
            return False
        return bool(re.search(
            r"\b(?:muốn|cần)\s+(?:mua|đặt|order)\b"
            r"|\b(?:mua|đặt\s+hàng|đặt\s+mua|order)\b.{0,30}\b(?:giúp|giùm|hộ)\b"
            r"|\b(?:có|bán)\b.{0,20}\b(?:loại|mẫu|size|màu)\s+(?:nào|gì)\b"
            r"|\b(?:những|các)\s+loại\s+(?:nào|gì)\b"
            r"|\bsản\s+phẩm\s+(?:gì|nào)\b"
            r"|\bcòn\s+hàng\s+(?:không|ko|k)\b"
            r"|\bgiá\s+(?:bao\s+nhiêu|thế\s+nào|sao)\b",
            value,
        ))

    @staticmethod
    def _asks_about_price(text: str) -> bool:
        value = " ".join(str(text or "").casefold().split())
        return bool(re.search(
            r"\bgiá\b|\bbao\s+nhiêu\s+tiền\b|\bnhiêu\s+tiền\b|\bgiá\s+(?:bao\s+nhiêu|thế\s+nào|sao)\b",
            value,
        ))

    @staticmethod
    def _asks_about_promotion(text: str) -> bool:
        value = " ".join(str(text or "").casefold().split())
        return ("giảm" in value and "%" in value) or any(phrase in value for phrase in (
            "giảm giá", "khuyến mãi", "khuyến mại", "ưu đãi",
            "mua một tặng một", "mua 1 tặng 1", "discount", "promotion",
        ))

    @staticmethod
    def _explicit_media_requests(text: str) -> list[MediaKind]:
        """Return media kinds explicitly requested in this one message.

        Mentions such as "tôi vừa gửi ảnh" describe inbound media; they are not
        permission to send a product asset back. Keep this deliberately narrow:
        a missed attachment can be handled by text, while an invented one is an
        irreversible send to the wrong customer context.

        One reader per written language, because the grammar they have to read
        is not the same: Burmese writes a request as one unspaced run of
        characters, so the Vietnamese word-boundary patterns can never fire
        on it. A shop set to `reply_language: my` answers in Burmese but is read
        here by both, since the setting pins our language, not the customer's.
        """
        value = " ".join(str(text or "").casefold().split())
        if not value:
            return []
        # Vietnamese first, and its refusals are final: a sentence it reads at
        # all ("không cần gửi ảnh") must not get a second verdict from a reader
        # that does not know the word "ảnh".
        return (
            MessageAnalyzer._vietnamese_media_requests(value)
            or MessageAnalyzer._burmese_media_requests(value)
        )

    @staticmethod
    def _vietnamese_media_requests(value: str) -> list[MediaKind]:
        media_pattern = (
            r"(?:ảnh|hình(?:\s+ảnh)?|minh\s+(?:họa|hoạ|hoa)|"
            r"photo|image|video|clip)"
        )
        if re.search(
            rf"\b(?:không|ko|k|chẳng)\s+(?:cần\s+)?(?:gửi|send)\b.{{0,40}}{media_pattern}"
            rf"|\b(?:đừng|khỏi)\s+(?:gửi|send)\b.{{0,40}}{media_pattern}",
            value,
        ):
            return []
        if re.search(
            rf"\b(?:tôi|mình|em|tớ|khách)\s+(?:vừa\s+|đã\s+|mới\s+)?"
            rf"(?:gửi|send)\b.{{0,40}}{media_pattern}"
            rf"|\b(?:shop|bạn|hệ thống)\s+(?:vừa|đã|mới)\s+"
            rf"(?:gửi|send)\b.{{0,40}}{media_pattern}",
            value,
        ):
            return []
        request = any(re.search(pattern, value) for pattern in (
            rf"\b(?:gửi|gui|send)\b.{{0,80}}{media_pattern}",
            rf"^(?:shop\s+)?(?:gửi|gui|send)\b.{{0,80}}{media_pattern}",
            rf"\bshop\s+(?:gửi|gui|send|cho)\b.{{0,80}}{media_pattern}",
            rf"^(?:cho)\s+(?:mình|tôi|em|tớ|chúng tôi)?\s*(?:xem|xin|nhận)\b"
            rf".{{0,80}}{media_pattern}",
            rf"\b(?:mình|tôi|em|tớ|chúng tôi)\s+(?:muốn|cần|xin)\b.{{0,40}}"
            rf"(?:xem|nhận|được)\b.{{0,40}}{media_pattern}",
            rf"\b(?:muốn|cần|xin)\s+(?:được\s+)?(?:xem|nhận)\b.{{0,50}}"
            rf"{media_pattern}",
            rf"^(?:shop\s+ơi[, ]*)?(?:có|cho)\b.{{0,40}}{media_pattern}"
            rf".{{0,60}}(?:không|ko|k|được không|được ko)?$",
            rf"\b(?:đâu|chưa\s+thấy|không\s+thấy|ko\s+thấy)\b.{{0,40}}"
            rf"{media_pattern}|{media_pattern}.{{0,30}}\bđâu\b",
        ))
        if not request:
            return []
        requested: list[MediaKind] = []
        if re.search(
            r"\b(?:ảnh|hình(?:\s+ảnh)?|minh\s+(?:họa|hoạ|hoa)|photo|image)\b",
            value,
        ):
            requested.append("image")
        if re.search(r"\b(?:video|clip)\b", value):
            requested.append("video")
        return requested

    # Burmese is written without reliable word spaces: "ဓာတ်ပုံပို့ပေးပါ"
    # (please send a photo) is one run of characters with no boundary for a
    # regex to anchor on, so these are substring matches. Latin loanwords stay
    # out of the verb lists on purpose - that is what keeps a Vietnamese
    # sentence from being re-read here after Vietnamese already refused it.
    _MY_IMAGE_WORDS = ("ဓာတ်ပုံ", "ရုပ်ပုံ", "ပုံ", "photo", "image")
    _MY_VIDEO_WORDS = ("ဗီဒီယို", "ဗွီဒီယို", "ဗီဒီယိုး", "video", "clip")
    # ပုံ is "picture" on its own ("ပုံလေး ပို့ပေးပါ", send a picture) but also
    # the "way of" suffix after a verb and part of unrelated words: how to use
    # / do / order / ship / say / apply / eat / wear, style, usual. Read as a
    # picture, "အသုံးပြုပုံ သိချင်ပါတယ်" (I want to know how to use it) handed
    # the customer to a person for a photo nobody asked for. Blanked out
    # before matching, so ပုံ is only read where it can still mean a picture.
    _MY_NOT_IMAGE_WORDS = (
        "အသုံးပြုပုံ", "သုံးပုံ", "လုပ်ပုံ", "မှာပုံ", "ပို့ပုံ", "ပြောပုံ",
        "လိမ်းပုံ", "စားပုံ", "ဝတ်ပုံ", "ပုံစံ", "ပုံမှန်",
    )
    # send / give / show / want to see / want to have / may I get. Not a bare
    # ချင် ("want"): that made "want to know" and "want to buy" requests.
    _MY_REQUEST_WORDS = (
        "ပို့", "ပေး", "ပြပေး", "ပြပါ", "ပြလို", "ကြည့်", "လိုချင်",
        "ရမလား", "ရနိုင်",
    )
    # "Ảnh đâu?" in Burmese: where is it / did not get it / cannot see it.
    _MY_MISSING_WORDS = ("ဘယ်မှာ", "မရ", "မမြင်")
    # Negation is မ + verb: don't send, don't give, don't need. Only these
    # three, because မကြည့်ရဘူး ("cannot view it") is a customer still waiting
    # for the file, exactly like "không thấy ảnh" on the Vietnamese side.
    _MY_REFUSAL_WORDS = ("မပို့", "မပေး", "မလို")
    # Past-tense send: the customer describing media they already sent us.
    _MY_INBOUND_WORDS = ("ပို့လိုက်", "ပို့ပြီး", "ပို့ထား", "ပို့တယ်", "ပို့ပါတယ်")

    @staticmethod
    def _burmese_media_requests(value: str) -> list[MediaKind]:
        for word in MessageAnalyzer._MY_NOT_IMAGE_WORDS:
            value = value.replace(word, " ")
        readers: tuple[tuple[MediaKind, tuple[str, ...]], ...] = (
            ("image", tuple(MessageAnalyzer._MY_IMAGE_WORDS)),
            ("video", tuple(MessageAnalyzer._MY_VIDEO_WORDS)),
        )
        kinds: list[MediaKind] = [
            kind for kind, words in readers if any(word in value for word in words)
        ]
        if not kinds:
            return []
        if any(word in value for word in MessageAnalyzer._MY_REFUSAL_WORDS):
            return []
        if any(word in value for word in MessageAnalyzer._MY_INBOUND_WORDS):
            return []
        asked = any(
            word in value
            for word in MessageAnalyzer._MY_REQUEST_WORDS
            + MessageAnalyzer._MY_MISSING_WORDS
        )
        return kinds if asked else []

    @staticmethod
    def _safe_media_followup(text: str) -> bool:
        """A product-media retry may bypass complaint routing only when benign."""
        value = " ".join(str(text or "").casefold().split())
        return not any(marker in value for marker in (
            "hoàn tiền", "trả hàng", "đổi trả", "thanh toán", "chuyển khoản",
            "lừa đảo", "pháp lý", "kiện", "dị ứng", "nguy hiểm", "tổn thương",
            # The same list in Burmese: refund, return the goods, exchange,
            # pay / bank transfer, scam, sue, allergic reaction, dangerous,
            # injured. Without these a Burmese refund complaint asking for a
            # photo would slip past the complaint routing below.
            "ပြန်အမ်း", "ပိုက်ဆံပြန်", "ပစ္စည်းပြန်", "လဲလှယ်", "ငွေပေးချေ",
            "ဘဏ်", "လိမ်", "တရားစွဲ", "ဓာတ်မတည့်", "အန္တရာယ်", "ထိခိုက်",
        ))

    @staticmethod
    def _is_address_change_request(text: str) -> bool:
        value = " ".join(str(text or "").casefold().split())
        return bool(re.search(
            r"\b(?:đổi|thay|cập\s*nhật|cập\s*nhập)\s+(?:lại\s+)?địa\s+chỉ\b",
            value,
        ))

    @staticmethod
    def _explicit_customer_facts(text: str) -> dict[str, str]:
        value = str(text or "").strip()
        if not value:
            return {}
        facts = {}
        name = re.search(
            r"(?:mình|tôi|em)\s+tên\s+(?P<value>[^,.;\n]{1,100})",
            value,
            flags=re.IGNORECASE,
        )
        if name:
            facts["name"] = name.group("value").strip()
        phone = re.search(
            r"(?:số\s+điện\s+thoại|điện\s+thoại|sđt|sdt|đt|phone|zalo|"
            r"liên\s+hệ(?:\s+qua)?)"
            r"\s*(?:của\s+(?:mình|tôi|em)\s*)?(?:là|:)?\s*"
            r"(?P<value>\+?[\d ()-]{9,24})",
            value,
            flags=re.IGNORECASE,
        )
        if phone:
            raw_phone = phone.group("value").strip()
            normalized = re.sub(r"[^\d+]", "", raw_phone)
            if "+" in normalized[1:]:
                normalized = ""
            digits = re.sub(r"\D", "", normalized)
            if 9 <= len(digits) <= 15:
                facts["phone"] = normalized
        email = re.search(
            r"(?<![\w.+-])(?P<value>[A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
            r"@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+)(?![\w.-])",
            value,
            flags=re.IGNORECASE,
        )
        if email:
            facts["email"] = email.group("value").lower()
        address = re.search(
            r"(?:nhận\s+hàng\s+tại|giao\s+hàng\s+tại|địa\s+chỉ\s*(?:là|:)?|đ/c\s*:?)"
            r"\s*(?P<value>[^\n]{5,500})",
            value,
            flags=re.IGNORECASE,
        )
        if address:
            address_value = re.split(
                r"\s*(?:[,.;|]|\s+-\s+)\s*(?="
                r"(?:số\s+điện\s+thoại|điện\s+thoại|sđt|sdt|đt|phone|"
                r"zalo|email|e-mail|mail|liên\s+hệ)\b)",
                address.group("value"),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" ,.;")
            address_value = re.sub(
                r"^(?:sang|thành|là)\s+", "", address_value,
                flags=re.IGNORECASE,
            ).strip()
            if re.match(
                r"^(?:với|và|cùng|mới|của\s+(?:mình|tôi|em)|sđt|sdt|"
                r"số\s+điện\s+thoại)\b",
                address_value,
                flags=re.IGNORECASE,
            ):
                address_value = ""
            if len(address_value) >= 5:
                facts["address"] = address_value[:250]
        if "address" not in facts:
            # A short reply like "mình ở cổ nhuế" carries an address without
            # any of the lead-in phrases above; keep the captured span to just
            # the place name so the stored fact is the address, not the
            # sentence around it.
            bare_location = re.search(
                r"\b(?:mình|tôi|em)\s+(?:hiện\s+(?:đang\s+)?|đang\s+)?ở\s+"
                r"(?P<value>[^\n,.;!?]{2,100})",
                value,
                flags=re.IGNORECASE,
            )
            if bare_location:
                candidate = bare_location.group("value").strip(" ,.;")
                stopwords = {"đây", "đó", "nhà", "ngoài", "trong", "xa", "gần", "kia"}
                if candidate and len(candidate) >= 3 and candidate.casefold() not in stopwords:
                    facts["address"] = candidate[:250]
        city = re.search(
            r"(?:thành\s+phố|tp\.?|tỉnh|city)\s*(?:là|:)?\s*"
            r"(?P<value>[^,.;|\n]{2,100})",
            value,
            flags=re.IGNORECASE,
        )
        if city:
            facts["city"] = city.group("value").strip()
        return facts

    @staticmethod
    def _facts_grounded_in_message(
        message: str, facts: dict[str, str], explicit_facts: dict[str, str],
    ) -> dict[str, str]:
        normalized_message = " ".join(str(message or "").casefold().split())
        grounded = {}
        for key, value in (facts or {}).items():
            if key in explicit_facts:
                grounded[key] = value
                continue
            normalized_value = " ".join(str(value or "").casefold().split())
            if normalized_value and normalized_value in normalized_message:
                grounded[key] = value
        return grounded

    @staticmethod
    def _is_multi_item_order(text: str) -> bool:
        value = " ".join(str(text or "").casefold().split())
        if not any(word in value for word in ("mua", "lấy", "đặt")):
            return False
        quantities = re.findall(
            r"\b\d+\s*(?:hộp|chai|gói|thùng|lon|túi|sản phẩm)\b",
            value,
        )
        return len(quantities) >= 2
