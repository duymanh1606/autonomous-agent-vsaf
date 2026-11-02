"""Accent-insensitive matching for the dashboard's global search (⌘K).

Operators type without diacritics far more often than with them ("nguyen",
"ao thun", "0912") and expect "Nguyễn", "Áo thun" to match. PostgreSQL's
`unaccent` extension would do this server-side but is not guaranteed to be
installed, so the matching happens in Python over bounded candidate sets
(newest N conversations, this account's orders, the product catalogue) -
all of which the dashboard already loads whole for their own pages, so
this costs nothing the list views do not already pay.
"""

import unicodedata


def fold(value) -> str:
    """Lower-case, strip diacritics, đ->d. `fold("Nguyễn Đức") == "nguyen duc"`."""
    if value is None:
        return ""
    text = str(value).replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.casefold()


def matches_words(words: list[str], *fields) -> bool:
    """True if every word in `words` (assumed already folded) occurs in the
    folded concatenation of `fields`."""
    if not words:
        return False
    haystack = " ".join(fold(field) for field in fields if field)
    return all(word in haystack for word in words)


def matches(term: str, *fields) -> bool:
    """True if every whitespace-separated word of `term` occurs (folded) in
    the folded concatenation of `fields` - so "nguyen 0912" finds a customer
    named Nguyễn whose phone starts 0912, in either order."""
    return matches_words(fold(term).split(), *fields)


def snippet(text: str, term_or_words: str | list[str], radius: int = 40) -> str:
    """The part of `text` around the first folded match of `term`'s first
    word, for showing *why* a message matched. Folding does not change
    string length for the characters involved (one code point in, one
    out), so folded offsets map back onto the original text directly."""
    if not text:
        return ""
    folded_text = fold(text)
    words = term_or_words if isinstance(term_or_words, list) else fold(term_or_words).split()
    position = -1
    for word in words:
        position = folded_text.find(word)
        if position >= 0:
            break
    if position < 0:
        return text[: radius * 2].strip()
    start = max(0, position - radius)
    end = min(len(text), position + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
