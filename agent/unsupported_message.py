"""Messages TikTok Web cannot show: voice messages and other app-only types.

TikTok Web renders such a message as a placeholder bubble -
"[Loại tin nhắn này không được hỗ trợ. Hãy tải về ứng dụng TikTok để xem tin
nhắn này.]" in Vietnamese, "This message type isn't supported. Download the
TikTok app to view this message." in English. The extension reads it like any
other text, so without this check the placeholder reached the model as if the
customer had typed it, and got answered as such.

Recognised by its wording only, in the two UI languages a workspace's Chrome
runs in. Matching is on the platform's own sentence, never on a customer's
words: a customer writing "tải app TikTok" is not a placeholder.
"""

import re
import unicodedata

_PATTERNS = tuple(re.compile(pattern) for pattern in (
    # Vietnamese UI
    r"loai tin nhan nay khong duoc ho tro",
    r"tai ve ung dung tiktok de xem tin nhan",
    # English UI
    r"this (?:type of )?message(?: type)? (?:is not|isn't|is unsupported|not) supported",
    r"this message type is(?:n't| not) supported",
    r"download the tiktok app to view (?:this|the) message",
    r"message type (?:is )?not supported",
))


def _fold(text: str) -> str:
    """Lower-case, accents and đ folded, curly quotes and spacing normalised."""
    value = str(text or "").casefold().replace("đ", "d").replace("’", "'")
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.split())


def is_unsupported_placeholder(text) -> bool:
    """True for TikTok Web's "this message type is not supported" bubble."""
    folded = _fold(text)
    return bool(folded) and any(pattern.search(folded) for pattern in _PATTERNS)
