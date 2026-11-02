"""Push customer state and order changes to an external Google Sheet.

One-way, best-effort mirror of two things an operator wants to see outside
the dashboard: where a conversation's customer stands (stage/intent) and
what has been ordered. Both live in unrelated write paths - customer state
comes from the agent pipeline (memory/conversation_store.py), orders come
from operator actions in the dashboard API - so each calls in here on its
own, keyed by conversation_id, and a Google Apps Script Web App on the
sheet side merges the two into one row.

Every call here must be safe to await from a real request/response or
message-handling path without ever blocking or raising - a Google outage
must never delay a reply to a customer or an operator's order save. Errors
are logged and swallowed, never propagated.
"""

import asyncio
import json
import logging
import os
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)


def _webhook() -> tuple[str, str]:
    """(url, token), read from the environment at call time.

    Not at import time: the agent imports this module (through
    storage/conversation_context.py) before main.py loads `.env`, so a value
    read at import was empty unless the supervisor happened to export it -
    the backfill script, which loads `.env` first, worked while the live
    export from every agent silently did nothing.
    """
    return (
        os.environ.get("GSHEET_WEBHOOK_URL", "").strip(),
        os.environ.get("GSHEET_WEBHOOK_TOKEN", "").strip(),
    )


def configured() -> bool:
    """Whether exports go anywhere - for a startup log line and scripts."""
    return bool(_webhook()[0])


# One doPost round trip is ~1.5-3s (execution + the redirect that serves
# its reply), and concurrent calls queue behind the script lock in
# webhook_receiver.gs - 5s was timing out under ordinary load.
_TIMEOUT_SECONDS = float(os.environ.get("GSHEET_TIMEOUT_SECONDS", "20"))

# Fire-and-forget tasks need a strong reference or asyncio can garbage
# collect them mid-flight - the same bug already fixed for sleep_wake's
# wake/sleep tasks (dashboard_api/sleep_wake.py's _spawn). This module has
# no long-lived controller instance to hold that reference on, so it keeps
# its own module-level set instead.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:
        # No running event loop (sync-only callers, e.g. one-off scripts):
        # the export is best-effort, so dropping it beats raising into the
        # write path that triggered it.
        coro.close()
        logger.warning("gsheet export skipped: no running event loop")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy-initialized persistent client that reuses TCP+TLS connections."""
    global _client
    if _client is None or _client.is_closed:
        # Apps Script runs doPost, then 302-redirects to a one-off URL that
        # serves its output. Without following, the write still happens but
        # the {"ok": false, "error": ...} body is never seen.
        _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
    return _client


# Apps Script stalls (then times out) when more than a few requests hit one
# deployment at once - 8 concurrent no-op calls already exceeded 30s - so
# in-flight calls are capped per process. Bound to the running loop and
# recreated if the loop changes (tests spin up one loop per case).
_MAX_IN_FLIGHT = 2
_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _in_flight_gate() -> asyncio.Semaphore:
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Semaphore(_MAX_IN_FLIGHT))
    return _gate[1]


async def close_client() -> None:
    """Gracefully close the shared client. Call on application shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# Apps Script answers a stray HTTP 404 / HTML page or stalls past the
# timeout every few calls even at one request at a time, so transient
# failures are retried. Safe because doPost upserts by conversation id.
_RETRY_DELAYS_SECONDS = (1.0, 3.0)


async def _post(payload: dict) -> bool:
    """True only when the webhook accepted the payload."""
    url, token = _webhook()
    if not url:
        return False  # export disabled until GSHEET_WEBHOOK_URL is configured
    conversation_id = payload.get("conversation_id")
    for attempt, delay in enumerate((*_RETRY_DELAYS_SECONDS, None)):
        accepted, transient_error = await _post_once(payload, url, token)
        if transient_error is None:
            return accepted
        if delay is None:
            logger.warning(
                "gsheet export gave up on conversation %s after %d attempts (last: %s)",
                conversation_id, attempt + 1, transient_error,
            )
            return False
        logger.info(
            "gsheet export retrying conversation %s in %.0fs: %s",
            conversation_id, delay, transient_error,
        )
        await asyncio.sleep(delay)
    return False


async def _post_once(payload: dict, url: str, token: str) -> tuple[bool, str | None]:
    """(accepted, transient_error): a transient error means "try again",
    while accepted=False with no error is a definitive rejection."""
    try:
        body = json.dumps({**payload, "token": token}, ensure_ascii=False)
    except (TypeError, ValueError):
        # A bug in the payload, not a network blip: retrying cannot help, and
        # reporting it as one hid that no order had ever reached the sheet.
        logger.exception(
            "gsheet export payload is not JSON for conversation %s",
            payload.get("conversation_id"),
        )
        return False, None
    try:
        async with _in_flight_gate():
            response = await _get_client().post(
                url, content=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:  # noqa: BLE001 - best-effort webhook; the caller reports the failure
        return False, type(exc).__name__
    # httpx does not raise for a non-2xx response on its own - without this
    # check a broken deployment (wrong URL, revoked access) "succeeds" from
    # this function's point of view forever.
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}"
    # webhook_receiver.gs always answers HTTP 200 and reports problems (bad
    # token, missing sheet, script errors) in the JSON body. A non-JSON body
    # is Google's own HTML page - a transient glitch, or the deployment has
    # no doPost / needs sign-in - also served as 200.
    try:
        body = response.json()
    except ValueError:
        return False, "non-JSON reply (is the deployment current and public?)"
    if isinstance(body, dict) and body.get("ok") is False:
        logger.warning(
            "gsheet export rejected for conversation %s: %s",
            payload.get("conversation_id"), body.get("error"),
        )
        return False, None
    logger.info(
        "gsheet export ok conversation=%s type=%s",
        payload.get("conversation_id"), payload.get("type"),
    )
    return True, None


# Last payload sent per conversation (minus updated_at). Every inbox scan
# re-persists identity for the thread on screen, which would otherwise mean
# one Apps Script execution per scan per conversation - and Apps Script
# meters total execution time per day, so identical re-sends have to be
# dropped here. Process-local on purpose: a restart re-sends once, harmless.
_last_exported: dict[str, tuple] = {}


def format_chat_address(address, city) -> str | None:
    """The address a customer gave in chat, with the city when it is separate."""
    address = " ".join(str(address or "").split())
    city = " ".join(str(city or "").split())
    if not address:
        return None
    if city and city.casefold() not in address.casefold():
        return f"{address}, {city}"
    return address


def product_interest(state) -> str | None:
    """"Áo Polo – size L – x2": the product with the size and quantity the
    customer gave, for the sheet's "Sp quan tâm" column. None without a
    product - a lone size or quantity names nothing."""
    name = str(getattr(state, "product_name", None) or "").strip()
    if not name:
        return None
    parts = [name]
    size = str(getattr(state, "size", None) or "").strip()
    if size:
        parts.append(f"size {size}")
    quantity = getattr(state, "quantity", None)
    if quantity:
        parts.append(f"x{quantity}")
    return " – ".join(parts)


async def export_customer_state(
    state, *, id_tiktok: str | None = None, customer_name: str | None = None,
    phone: str | None = None, chat_address: str | None = None,
    shop: str | None = None,
) -> None:
    """`id_tiktok`/`customer_name`/`phone` come from customer_profile
    (populated by DOM scraping - storage/conversation_context.py), not from
    `state` itself: CustomerState tracks conversation stage/intent, not who
    the customer is. All three are None until scraped, and are sent as-is
    (blank on the sheet) rather than skipped - the Apps Script side
    (integrations/apps_script/webhook_receiver.gs) only overwrites a cell
    for a field actually present in the payload, so a blank here never wipes
    out a name/phone the sheet already learned some other way.

    `chat_address` is the address the customer gave in chat. The sheet's
    address column belongs to the order (export_order), and this payload is
    re-sent on every inbox scan, so the Apps Script writes it only into an
    empty cell - it fills the gap before an order, never overwrites one."""
    interest = product_interest(state)
    fingerprint = (
        shop, id_tiktok, state.customer_id, customer_name, phone, chat_address,
        state.stage, state.intent, interest,
    )
    if _last_exported.get(state.conversation_id) == fingerprint:
        return
    accepted = await _post({
        "type": "customer",
        # Shown in column A; the row is still found by conversation_id.
        "shop": shop,
        "conversation_id": state.conversation_id,
        "id_tiktok": id_tiktok,
        "customer_id": state.customer_id,
        "customer_name": customer_name,
        "phone": phone,
        "chat_address": chat_address,
        "stage": state.stage,
        "intent": state.intent,
        "product_name": interest,
        "updated_at": state.updated_at.isoformat(),
    })
    if accepted:
        _last_exported[state.conversation_id] = fingerprint


def _number(value):
    """A NUMERIC column comes back from psycopg as Decimal, which JSON cannot
    carry - every order with a total failed to export because of it."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


async def export_order(order: dict, *, shop: str | None = None) -> None:
    conversation_id = order.get("conversation_id")
    if not conversation_id:
        return  # nothing on the sheet to match this order to
    updated_at = order.get("updated_at")
    await _post({
        "type": "order",
        "shop": shop,
        "conversation_id": conversation_id,
        # Looked up by the caller (dashboard_api/routes/orders.py) from
        # customer_profile via this same conversation_id - orders themselves
        # don't store it.
        "id_tiktok": order.get("id_tiktok"),
        "customer_name": order.get("customer_name"),
        "order_status": order.get("status"),
        "order_items": order.get("items"),
        "order_total": _number(order.get("total_amount")),
        "shipping_address": order.get("shipping_address"),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    })


def spawn_export_customer_state(
    state, *, id_tiktok: str | None = None, customer_name: str | None = None,
    phone: str | None = None, chat_address: str | None = None,
    shop: str | None = None,
) -> None:
    """For callers already inside a running event loop with no request/response
    cycle to hang a background task off of - the agent pipeline's save_state()."""
    _spawn(export_customer_state(
        state, id_tiktok=id_tiktok, customer_name=customer_name, phone=phone,
        chat_address=chat_address, shop=shop,
    ))
