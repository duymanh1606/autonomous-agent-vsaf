import re
import unicodedata

from agent.reply_language import is_wrong_language
from models.analysis import MessageAnalysis
from models.reply import ReplyResult, ReplyValidation
from models.tool import ToolResult


# Matches a currency amount the reply states, in either language the agent
# writes in (messaging.reply_language, agent/reply_language.py):
#   Vietnamese - "259.000 đồng", "259.000đ", "350k", "3 triệu"
#   Burmese    - "259000 ကျပ်", "25,000 Ks", "12000 MMK", "220,000 kyats", "3 သိန်း"
# Grouped thousands (259.000) or four-plus bare digits (259000) read as a
# whole amount; "k"/"nghìn"/"ngàn"/"triệu" and Burmese "သိန်း" (lakh,
# 100,000) are the shorthand multipliers customers and the model both use in
# chat. A Burmese price is rarely abbreviated, so three bare digits already
# count there.
_PRICE_MENTION_RE = re.compile(
    r"(?P<amount>\d{1,3}(?:[.,]\d{3})+|\d{4,})\s*(?:đồng|vnđ|vnd|đ\b)"
    r"|(?P<mm_amount>\d{1,3}(?:[.,]\d{3})+|\d{3,})\s*(?:ကျပ်|ks\b|mmk|kyats?\b)"
    r"|(?P<short_amount>\d+(?:[.,]\d+)?)\s*(?P<unit>k\b|nghìn|ngàn|triệu|သိန်း)",
    re.IGNORECASE,
)
_UNIT_MULTIPLIERS = {
    "k": 1_000, "nghìn": 1_000, "ngàn": 1_000, "triệu": 1_000_000,
    "သိန်း": 100_000,
}

# Burmese writes prices in Myanmar digits as readily as in ASCII ones, and
# int() cannot parse them, so every reply is normalised before matching.
_MYANMAR_DIGITS = str.maketrans({chr(0x1040 + value): str(value) for value in range(10)})


def _fold(text: str) -> str:
    """Lower-case, accents stripped, đ as d, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


_MEDIA = r"(?:hinh|anh|video|clip|photos?|images?|pictures?|videos?)"
# The shop saying it cannot send pictures or videos. Always false - the
# system sends catalogue media whenever a customer asks and the product has
# some, and hands the thread to a person when it has none - and seen in
# production sent in the same breath as the photo it claimed it could not
# send. Worded to need "hỗ trợ", "thể" or "được" so that "bạn chưa gửi ảnh"
# (the customer has not sent one yet) is not caught.
_DENIES_MEDIA = re.compile("|".join((
    rf"(?:chua|khong)\s+(?:co\s+)?ho\s*tro\s+(?:viec\s+)?gui\s+(?:\w+\s+){{0,2}}{_MEDIA}",
    rf"(?:chua|khong)\s+the\s+gui\s+(?:\w+\s+){{0,2}}{_MEDIA}",
    rf"khong\s+gui\s+duoc\s+(?:\w+\s+){{0,2}}{_MEDIA}",
    rf"gui\s+(?:\w+\s+){{0,3}}{_MEDIA}\s+(?:\w+\s+){{0,4}}(?:chua|khong)\s+(?:duoc\s+)?ho\s*tro",
    rf"(?:cannot|can't|can not|unable to|not able to)\s+send\s+(?:\w+\s+){{0,2}}{_MEDIA}",
)))


def denies_media_capability(reply: str) -> bool:
    return bool(_DENIES_MEDIA.search(_fold(reply)))


# Saying a product is gone is the one thing a reply may say about it.
_NO_LONGER_SOLD = re.compile(
    r"ngung\s+(?:ban|kinh\s+doanh|san\s+xuat)|khong\s+con\s+(?:ban|kinh\s+doanh|hang|san\s+pham)"
    r"|het\s+kinh\s+doanh|da\s+(?:go|xoa)|no\s+longer\s+(?:sold|available)|discontinued"
)


def mentions_removed_product(reply: str, removed_products) -> bool:
    """The reply names a product the shop no longer sells, other than to say so."""
    text = _fold(reply)
    names = [_fold(name) for name in removed_products if len(_fold(name)) >= 3]
    return any(name in text for name in names) and not _NO_LONGER_SOLD.search(text)


class ReplyValidator:
    def __init__(self, max_characters=1000):
        self.max_characters = int(max_characters)

    def validate(
        self,
        reply: ReplyResult,
        analysis: MessageAnalysis,
        tool_result: ToolResult | None,
        last_outbound_text: str | None,
        attachments=(),
        removed_products=(),
        reply_language=None,
    ) -> ReplyValidation:
        text = reply.text.strip()
        if not reply.should_send:
            return ReplyValidation(allowed=False, reason="reply_generator_declined")
        if reply.requires_human or analysis.requires_human:
            return ReplyValidation(allowed=False, reason="human_review_required")
        if not text:
            return ReplyValidation(allowed=False, reason="empty_reply")
        if len(text) > self.max_characters:
            return ReplyValidation(allowed=False, reason="reply_too_long")
        if analysis.requires_tool and (tool_result is None or not tool_result.success):
            return ReplyValidation(allowed=False, reason="required_tool_not_verified")
        if self._has_unverified_price_claim(text, tool_result):
            # The prompt tells the model to quote a price only from
            # verified_tool_result, but that is an instruction, not a
            # guarantee - seen in production quoting a price nothing in the
            # tool result ever stated (e.g. check_inventory, which carries no
            # price field at all). A stated amount that matches no verified
            # number is therefore never sent, regardless of why the model
            # wrote it.
            return ReplyValidation(allowed=False, reason="unverified_price_claim")
        # A raw path or a URL naming a media file is never legitimate in a
        # reply's text - TikTok DM has no inline-link mechanism, so this is
        # always either a leaked local path or a fabricated link (a model
        # asked not to write "images/x.png" has been observed inventing
        # "https://cdn.example.com/images/x.png" instead). This check is
        # therefore unconditional, unlike the word-claim one below, which
        # stays fine to say once a real attachment is actually being sent.
        if self._references_media_by_path_or_link(text):
            return ReplyValidation(allowed=False, reason="claims_missing_attachment")
        if not attachments and self._claims_missing_attachment(text):
            return ReplyValidation(allowed=False, reason="claims_missing_attachment")
        if denies_media_capability(text):
            return ReplyValidation(allowed=False, reason="denies_media_capability")
        if mentions_removed_product(text, removed_products):
            return ReplyValidation(allowed=False, reason="mentions_removed_product")
        if self._asks_for_known_address(text, analysis):
            return ReplyValidation(allowed=False, reason="asks_for_known_address")
        if reply_language is not None and is_wrong_language(reply_language, text):
            # The generator already forced one rewrite; a draft still in the
            # wrong language is never sent. The shop speaks only its
            # configured language, so the thread goes to a person instead.
            # After the address check: that one is answered with a canned line
            # already in the right language, which is re-validated here.
            return ReplyValidation(allowed=False, reason="wrong_reply_language")
        if last_outbound_text and self._normalize(text) == self._normalize(last_outbound_text):
            return ReplyValidation(allowed=False, reason="duplicate_reply")
        return ReplyValidation(allowed=True)

    @classmethod
    def _references_media_by_path_or_link(cls, reply: str) -> bool:
        normalized = cls._normalize(reply)
        media_extension = r"\.(?:jpe?g|png|webp|gif|mp4|mov|webm)"
        pattern = (
            # Any directory-looking path ending in a media file, wherever it
            # sits in the sentence: "images/x.png", "/images/x.png",
            # "products/sp1/a.jpg" - a leading space used to let it through.
            r"(?:^|[\s\[/\\(:])[^\s\]\[()]*/[^\]\s()]+" + media_extension
            + r"|\[[^\]]+" + media_extension + r"\]"
            # Any http(s) link ending in a media extension - real or, as
            # observed in production, invented wholesale by the model.
            + r"|https?://\S+" + media_extension
        )
        return bool(re.search(pattern, normalized))

    @classmethod
    def _claims_missing_attachment(cls, reply: str) -> bool:
        normalized = cls._normalize(reply)
        return any(marker in normalized for marker in (
            "đây là hình ảnh", "đây là ảnh", "dưới đây là hình", "dưới đây là ảnh",
            "ảnh đính kèm", "hình đính kèm", "video đính kèm",
            "image attached", "photo attached", "video attached",
        ))

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    @classmethod
    def _has_unverified_price_claim(cls, reply: str, tool_result: ToolResult | None) -> bool:
        mentions = cls._extract_price_mentions(reply)
        if not mentions:
            return False
        if tool_result is None or not tool_result.success:
            return True
        verified = cls._numeric_values(tool_result.data) | cls._verified_totals(tool_result.data)
        return any(amount not in verified for amount in mentions)

    # A reply may state "2 boxes x 124,000 + 6,000 shipping = 254,000": a total
    # nobody stored, built only from verified amounts. Bounded so the set of
    # acceptable numbers stays small: a made-up amount is still refused.
    MAX_TOTAL_QUANTITY = 20
    _PRICE_KEYS = frozenset({"price"})
    _FEE_KEYS = frozenset({"shipping_fee"})

    @classmethod
    def _keyed_amounts(cls, data, keys) -> set[int]:
        found = set()
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in keys and isinstance(item, (int, float)) and not isinstance(item, bool):
                        found.add(round(item))
                    else:
                        stack.append(item)
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
        return found

    @classmethod
    def _verified_totals(cls, data) -> set[int]:
        """quantity x a verified price, with or without a verified shipping fee."""
        prices = {price for price in cls._keyed_amounts(data, cls._PRICE_KEYS) if price > 0}
        fees = cls._keyed_amounts(data, cls._FEE_KEYS)
        subtotals = {
            price * quantity
            for price in prices
            for quantity in range(1, cls.MAX_TOTAL_QUANTITY + 1)
        }
        return subtotals | {subtotal + fee for subtotal in subtotals for fee in fees}

    @classmethod
    def _extract_price_mentions(cls, reply: str) -> set[int]:
        amounts = set()
        for match in _PRICE_MENTION_RE.finditer(reply.translate(_MYANMAR_DIGITS)):
            stated = match.group("amount") or match.group("mm_amount")
            if stated:
                amounts.add(int(re.sub(r"[.,]", "", stated)))
                continue
            multiplier = _UNIT_MULTIPLIERS[match.group("unit").casefold()]
            amounts.add(round(float(match.group("short_amount").replace(",", ".")) * multiplier))
        return amounts

    @classmethod
    def _numeric_values(cls, data) -> set[int]:
        """Every number verified_tool_result actually states, at any depth.

        Schema-agnostic on purpose: business tools are injected per shop
        (`get_shipping`/`get_order`/`get_customer` are Protocols with no
        fixed field names here), so this walks the whole payload rather than
        assuming a `price` key.
        """
        found = set()
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                found.add(round(value))
            elif isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
        return found

    @classmethod
    def _asks_for_known_address(cls, reply: str, analysis: MessageAnalysis) -> bool:
        normalized = cls._normalize(reply)
        asks_address = "địa chỉ" in normalized and any(
            phrase in normalized for phrase in (
                "cung cấp", "cho em xin", "cho mình xin", "vui lòng cho",
                "gửi lại", "xin địa chỉ",
            )
        )
        if not asks_address:
            return False
        verified = analysis.verified_customer_context or {}
        facts = verified.get("customer_facts") or {}
        if facts.get("address"):
            return True
        history = verified.get("recent_messages") or ()
        return any(
            "địa chỉ" in cls._normalize(item.get("text", ""))
            or any(token in cls._normalize(item.get("text", "")) for token in (
                "nhận hàng tại", "giao hàng tại", "đường ", "phố ", "quận ",
            ))
            for item in history if isinstance(item, dict)
        )
