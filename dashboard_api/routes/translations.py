"""Vietnamese translations under the bubbles of a non-Vietnamese shop's thread.

See dashboard_api/translations.py for what is cached and why it is display
only. The request names a conversation, never a text: what gets translated
is read back from this workspace's own messages, so the route cannot be used
as a free translation service on the workspace's Gemini key.
"""

import asyncio
import logging

import httpx
import psycopg
from fastapi import APIRouter

from agent.reply_language import (
    DEFAULT_REPLY_LANGUAGE, REPLY_LANGUAGES, normalize_language,
)
from dashboard_api.translations import TARGET_LANGUAGE, text_hash, translate_texts


logger = logging.getLogger(__name__)

# Only when a workspace config names no llm.model at all.
DEFAULT_TRANSLATION_MODEL = "gemini-3.1-flash-lite"

router = APIRouter()

# Gemini calls one request may make room for: the newest texts first, so the
# part of the thread on screen is translated before the history above it.
# The rest is reported as `remaining` and picked up by the next request.
MAX_TRANSLATED_PER_REQUEST = 60

# One translation run per workspace at a time. A second request for the same
# thread (the page polls) waits, then finds the first one's work in the cache
# instead of paying for it again.
_locks: dict[str, asyncio.Lock] = {}


def _main():
    # Late, for the same reason as routes/orders.py.
    from dashboard_api import main

    return main


def _result(enabled: bool, translations=(), remaining: int = 0, error: str | None = None):
    return {
        "enabled": enabled, "translations": list(translations),
        "remaining": remaining, "error": error,
    }


@router.post("/api/accounts/{account_id}/conversations/{conversation_id:path}/translations")
async def translate_conversation(account_id: str, conversation_id: str):
    main = _main()
    account = main._account_or_404(account_id)
    language = normalize_language(
        main.registry.config(account).get("messaging.reply_language", DEFAULT_REPLY_LANGUAGE)
    )
    if language == TARGET_LANGUAGE:
        return _result(False)
    # Only the shop's own script: a Vietnamese line an operator typed, an
    # emoji or an order number has nothing to translate.
    source = REPLY_LANGUAGES[language]

    detail = await asyncio.to_thread(main._reader(account).conversation, conversation_id)
    wanted = [
        message for message in detail.get("messages") or []
        if source.has_script(message.get("text") or "")
    ]
    # Newest first, one entry per distinct text.
    texts_by_hash: dict[str, str] = {}
    for message in reversed(wanted):
        texts_by_hash.setdefault(text_hash(message["text"]), message["text"].strip())

    store = main.translation_store
    error = None
    try:
        cached = await asyncio.to_thread(store.get_many, account.id, list(texts_by_hash))
        missing = [hashed for hashed in texts_by_hash if hashed not in cached]
        if missing and not account.api_key:
            error = "Workspace chưa có Gemini API key nên chưa dịch được"
        elif missing:
            async with _locks.setdefault(account.id, asyncio.Lock()):
                # Whatever a request that held the lock before us just did.
                cached.update(await asyncio.to_thread(store.get_many, account.id, missing))
                todo = [hashed for hashed in missing if hashed not in cached]
                todo = todo[:MAX_TRANSLATED_PER_REQUEST]
                if todo:
                    error = await _translate(account, language, texts_by_hash, todo, cached)
    except psycopg.Error as exc:
        logger.warning("[translate] cache unavailable account=%s error=%s", account.id, exc)
        return _result(True, error="Chưa dịch được lúc này, thử lại sau")

    translations = [
        {
            "message_id": message["message_id"],
            "direction": message["direction"],
            "text": cached[text_hash(message["text"])],
        }
        for message in wanted
        if text_hash(message["text"]) in cached
    ]
    remaining = sum(1 for hashed in texts_by_hash if hashed not in cached)
    return _result(True, translations, remaining, error)


async def _translate(account, language, texts_by_hash, todo, cached) -> str | None:
    """Translate `todo`, store and merge what came back; an error message or None."""
    # The same model the workspace answers customers with (configs/config.yaml).
    model = _main().registry.config(account).get("llm.model", DEFAULT_TRANSLATION_MODEL)
    try:
        done = await translate_texts(
            [texts_by_hash[hashed] for hashed in todo],
            source_language=language, api_key=account.api_key,
            model=model, account_id=account.id,
        )
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        logger.warning("[translate] gemini failed account=%s error=%s", account.id, exc)
        return "Chưa dịch được lúc này, thử lại sau"
    fresh = {todo[index]: translated for index, translated in done.items()}
    if not fresh:
        # Reported as an error, not as `remaining`: the page keeps asking
        # while there is progress to make, and a text the model will not
        # translate would otherwise be paid for on every poll.
        return "Chưa dịch được lúc này, thử lại sau"
    await asyncio.to_thread(
        _main().translation_store.put_many, account.id, fresh, model=model,
    )
    cached.update(fresh)
    return None
