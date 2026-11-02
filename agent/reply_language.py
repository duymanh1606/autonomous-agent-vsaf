"""Which language the agent writes its replies in.

One shop = one language: the operator picks it in Dashboard > Kịch bản and
every reply prompt is written in that language, even when the customer wrote
in another one. That is deliberate - a Myanmar shop answering a Vietnamese
browser in Vietnamese is exactly the mix-up this setting exists to prevent.

Each language carries its own script check so the generator can catch the
real production defect (Gemini answering in generic English) without any
hardcoded "Vietnamese" left in the prompt code.
"""

import unicodedata

# Fallback when a config carries no `messaging.reply_language` - workspaces
# created before the setting existed, all Vietnamese shops - and for an unknown
# code. A NEW workspace gets the base config's value instead (configs/config.yaml,
# Myanmar), which dashboard_api/accounts.py copies into its YAML.
DEFAULT_REPLY_LANGUAGE = "vi"

# An all-ASCII reply must contain enough letters before a script check means
# anything: "OK", a number or an emoji is neutral in every language.
MIN_LETTERS_TO_JUDGE = 8


def _is_vietnamese_letter(char: str) -> bool:
    """True for a Latin letter carrying a Vietnamese diacritic, and đ/Đ.

    Decomposition-based rather than a hardcoded character list, so every
    accented Vietnamese letter is covered without enumerating them. The mark
    must sit on a Latin letter: Burmese asat (U+103A) and virama (U+1039) are
    combining marks too, and once made every Burmese reply look Vietnamese.
    """
    if char in "đĐ":
        return True
    parts = unicodedata.normalize("NFD", char)
    return (
        len(parts) > 1
        and "LATIN" in unicodedata.name(parts[0], "")
        and any(unicodedata.combining(part) for part in parts[1:])
    )


def _is_myanmar_letter(char: str) -> bool:
    """True for a letter of the Myanmar (Burmese) script.

    Covers the Myanmar block and its two extensions. Letters only: Myanmar
    digits and the ၊ ။ punctuation prove nothing about the language, and one
    "၃" once passed a Vietnamese delivery estimate as Burmese.
    """
    return (
        "က" <= char <= "႟" or "ꩠ" <= char <= "ꩿ" or "ꧠ" <= char <= "꧿"
    ) and char.isalpha()


class _Language:
    def __init__(
        self, code: str, label: str, prompt_name: str, is_letter,
        handoff_ack: str, address_ack: str, request_ack: str, unsupported_ack: str,
        contact_request: str, default_style=(),
    ):
        self.code = code
        # Shown in the dashboard picker.
        self.label = label
        # How the language is named to the model, in English.
        self.prompt_name = prompt_name
        # Is this one character a letter of the language's own script?
        self.is_letter = is_letter
        # The two replies no model writes: they are sent when the pipeline has
        # decided not to trust a generated one, so they cannot come from the
        # LLM. Hardcoded in Vietnamese, they were the one thing a Burmese shop
        # still said in Vietnamese - and they are what a customer sees at the
        # worst moment, a handoff.
        self.handoff_ack = handoff_ack
        # Added under the handoff line when the shop has no phone number for
        # the customer yet, so the person taking over has a way to follow up.
        self.contact_request = contact_request
        self.address_ack = address_ack
        # The courtesy line after accepting a TikTok message request, used
        # when the operator-configured one is not in this language.
        self.request_ack = request_ack
        # Sent when a customer's message is one TikTok Web cannot show (a
        # voice message...) and a person takes over; used when the configured
        # `messaging.unsupported_message_reply` is not in this language.
        self.unsupported_ack = unsupported_ack
        # The tone/layout rules a shop of this language gets until an operator
        # writes its own (`messaging.reply_style`). Per language because
        # politeness is: Vietnamese rules about "anh/chị" and "Dạ" made a
        # Myanmar shop's prompt pull its replies towards Vietnamese.
        self.default_style = tuple(default_style)

    def has_script(self, text: str) -> bool:
        return any(self.is_letter(char) for char in text)


# Ordered: the dashboard renders the picker in this order.
REPLY_LANGUAGES: dict[str, _Language] = {
    language.code: language
    for language in (
        _Language(
            "vi", "Tiếng Việt", "Vietnamese", _is_vietnamese_letter,
            handoff_ack=(
                "Dạ, shop đã nhận được yêu cầu của bạn và đang chuyển nhân viên "
                "phụ trách kiểm tra để hỗ trợ chính xác hơn. Shop sẽ phản hồi "
                "bạn sớm nhất có thể ạ."
            ),
            contact_request=(
                "Bạn vui lòng cung cấp thông tin liên hệ (số điện thoại) để shop "
                "tư vấn chi tiết hơn ạ."
            ),
            address_ack=(
                "Dạ, shop đã nhận được thông tin nơi nhận hàng của bạn. "
                "Shop tiếp tục hỗ trợ yêu cầu hiện tại trong cuộc trò chuyện này ạ."
            ),
            request_ack="Dạ shop đã nhận được tin nhắn, mình kiểm tra và phản hồi ngay ạ.",
            unsupported_ack="Vui lòng đợi trong giây lát, nhân viên sẽ trả lời bạn.",
            default_style=(
                "Giọng lịch sự, rõ ràng: gọi khách là 'anh/chị' (hoặc 'bạn' nếu khách xưng vậy), tự xưng 'em' hoặc 'shop'.",
                "Câu ngắn, mỗi câu tối đa khoảng 15-20 từ; mỗi câu chỉ một ý.",
                "Mỗi tin tối đa 3 câu, mỗi câu xuống một dòng; không viết thành đoạn dài.",
                "Kết thúc bằng tối đa một câu hỏi, và chỉ khi câu hỏi giúp chốt đơn.",
                "'Dạ' tối đa một lần ở đầu tin; không lặp lại tên khách, không nhắc lại điều khách vừa nói.",
                "Không emoji, không dấu chấm than, không câu đệm thừa, không lặp ý.",
            ),
        ),
        _Language(
            "my", "Tiếng Myanmar (Burmese)", "Burmese (Myanmar)", _is_myanmar_letter,
            handoff_ack=(
                "ဆိုင်မှ သင့်တောင်းဆိုမှုကို လက်ခံရရှိပါသည်။ ပိုမိုတိကျစွာ "
                "ကူညီနိုင်ရန် တာဝန်ရှိဝန်ထမ်းထံ လက်ဆင့်ကမ်းပေးထားပါသည်။ "
                "အမြန်ဆုံး ပြန်လည်အကြောင်းကြားပါမည်။"
            ),
            contact_request=(
                "ပိုမိုအသေးစိတ် အကြံပြုပေးနိုင်ရန် ဆက်သွယ်ရမည့် "
                "ဖုန်းနံပါတ်ကို ပေးပို့ပေးပါရှင်။"
            ),
            address_ack=(
                "ဆိုင်မှ သင့်ပစ္စည်းလက်ခံမည့် လိပ်စာကို လက်ခံရရှိပါသည်။ "
                "ဤစကားဝိုင်းတွင် လက်ရှိတောင်းဆိုမှုကို ဆက်လက် ကူညီပေးပါမည်။"
            ),
            request_ack=(
                "ဆိုင်မှ သင့်စာကို လက်ခံရရှိပါသည်။ စစ်ဆေးပြီး "
                "အမြန်ဆုံး ပြန်လည်ဖြေကြားပေးပါမည်။"
            ),
            unsupported_ack=(
                "ခဏစောင့်ပေးပါ။ ဝန်ထမ်းမှ ပြန်လည်ဖြေကြားပေးပါမည်။"
            ),
            # Written in English on purpose: an instruction in Vietnamese (or
            # quoting Vietnamese words) is exactly what pulled replies away
            # from Burmese.
            default_style=(
                "Polite, warm, natural spoken Burmese, as a friendly shop assistant "
                "addressing a customer respectfully.",
                "Close sentences with a polite particle (ရှင် by default) and keep it "
                "consistent within a reply.",
                "Short sentences, one idea each.",
                "At most 3 sentences per message, each on its own line; never a long paragraph.",
                "End with at most one question, and only when it helps close the order.",
                "Do not repeat the customer's name or restate what they just said.",
                "No emoji, no exclamation marks, no filler, no repetition.",
            ),
        ),
    )
}


def normalize_language(value) -> str:
    """The configured code, or the default for anything unrecognised.

    An unknown code is a config typo, not a reason to stop answering
    customers: the shop keeps replying in the default language instead.
    """
    code = str(value or "").strip().lower()
    return code if code in REPLY_LANGUAGES else DEFAULT_REPLY_LANGUAGE


def language_name(code) -> str:
    return REPLY_LANGUAGES[normalize_language(code)].prompt_name


def language_label(code) -> str:
    return REPLY_LANGUAGES[normalize_language(code)].label


def language_instruction(code) -> str:
    """The sentence that pins the reply language in every prompt."""
    name = language_name(code)
    return (
        f"Write the reply in {name}. Always write in {name}, even when the customer "
        f"wrote in another language: never switch to the customer's language. "
    )


def unsupported_message_ack(code, configured=None) -> str:
    """What a customer is sent when their message cannot be read on the web.

    The configured text when it is in the shop's language, else the
    language's own line - a Vietnamese default must not reach a Myanmar shop.
    """
    return (
        fixed_text_in_language(code, configured)
        or REPLY_LANGUAGES[normalize_language(code)].unsupported_ack
    )


def default_reply_style(code) -> list[str]:
    """The tone/layout rules for a shop of this language that set none."""
    return list(REPLY_LANGUAGES[normalize_language(code)].default_style)


def handoff_ack(code, ask_contact=False) -> str:
    """The courtesy line sent while a person takes the thread over.

    `ask_contact`: also ask for a phone number, on its own line - for a
    customer the shop has none for yet.
    """
    language = REPLY_LANGUAGES[normalize_language(code)]
    if ask_contact:
        return f"{language.handoff_ack}\n{language.contact_request}"
    return language.handoff_ack


def address_ack(code) -> str:
    """Sent instead of asking again for an address we have already verified."""
    return REPLY_LANGUAGES[normalize_language(code)].address_ack


def request_ack(code) -> str:
    """Sent right after accepting a message request (see inbox_watcher)."""
    return REPLY_LANGUAGES[normalize_language(code)].request_ack


def fixed_text_in_language(code, text) -> str:
    """`text` when it is in the shop's language, else "".

    For the operator-written lines sent without the LLM (message-request ack,
    follow-up rules): written once in one language, they would otherwise keep
    going out in it after the shop switches language.
    """
    text = str(text or "").strip()
    return "" if not text or is_wrong_language(code, text) else text


def is_wrong_language(code, reply_text: str) -> bool:
    """True when the draft is clearly not in the configured language.

    Script-based. Judged on the letters outside ASCII: most of them must be
    the language's own. One stray character no longer decides it - a Myanmar
    digit in a Vietnamese sentence, a Burmese mark read as a diacritic - while
    a Latin product name ("Nike Air") never counts against a Burmese reply.

    A reply with no such letter at all (English, typically) is judged only
    when long enough: a short or neutral reply (a number, "OK", an emoji) is
    never flagged.
    """
    language = REPLY_LANGUAGES[normalize_language(code)]
    letters = [char for char in unicodedata.normalize("NFC", reply_text) if char.isalpha()]
    foreign = [char for char in letters if not char.isascii()]
    if foreign:
        own = sum(1 for char in foreign if language.is_letter(char))
        return own * 2 < len(foreign)
    return len(letters) >= MIN_LETTERS_TO_JUDGE
