"""Reply generation seam for the extension-driven runtime.

The UI controller never talks to an LLM directly. It asks a `ReplyProvider` for
one decision about one inbound message, so the browser flow stays testable and
runnable without any model credentials.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Protocol

from agent.message_router import MessageRoute
from agent.product_lookup import ProductLookupPolicy
from agent.reply_timing import ReplyTimer
from agent.reply_language import (
    address_ack,
    handoff_ack,
    normalize_language,
    unsupported_message_ack,
)
from agent.shop_policy import ShopPolicy
from agent.unsupported_message import is_unsupported_placeholder
from agent.tool_policy import tool_is_available, without_tool
from models.conversation import ConversationMessage
from models.message_event import MessageDirection, MessageEvent
from models.outbound_message import Attachment
from models.reply import ReplyResult
from models.tool import ToolResult


logger = logging.getLogger(__name__)

# How many conversations the in-memory fallbacks keep. They exist for the
# credential-free path that runs without a database; with a store configured the
# state and the last answer are read from it, and these caches would otherwise
# grow with every customer the agent ever served.
MEMORY_CACHE_LIMIT = 200


@dataclass
class ConversationContext:
    platform: str
    conversation_key: str
    message_id: str
    message_text: str
    customer_name: str | None = None
    avatar_url: str | None = None
    history: list[dict] = field(default_factory=list)
    # The unanswered burst this reply covers. `message_text` is their joined
    # text and `message_id` the last one, which is what the send signature and
    # the "already replied?" checks key on.
    pending_messages: list[dict] = field(default_factory=list)
    inbound_media: list[dict] = field(default_factory=list)
    # Where this reply's time goes (agent/reply_timing.py); the inbox scan
    # passes its own so the pipeline's steps land beside open/hold/send.
    timer: ReplyTimer = field(default_factory=ReplyTimer, compare=False, repr=False)


# The handoff reason for a message TikTok Web cannot show; the dashboard
# notification explains it (dashboard_api/notifications.py).
UNSUPPORTED_MESSAGE_REASON = "unsupported_message_type"
# The handoff reason once an ordering customer has given phone and address.
ORDER_READY_REASON = "order_ready"


@dataclass(frozen=True)
class ReplyDecision:
    text: str = ""
    should_send: bool = False
    requires_human: bool = False
    reason: str = ""
    model: str | None = None
    attachments: tuple[Attachment, ...] = ()
    intent: str = ""
    conversation_stage: str = ""
    product_ids: tuple[str, ...] = ()
    source_facts: tuple[dict, ...] = ()
    inbound_watermark: str = ""
    # What the chain concluded on the way to this decision. Callers that have to
    # record the outcome - an escalation row, a customer-state row - need these
    # and used to re-run the whole chain themselves to get them.
    analysis: object | None = None
    state: object | None = None

    @classmethod
    def human(cls, reason: str, *, analysis=None, state=None) -> "ReplyDecision":
        return cls(
            should_send=False, requires_human=True, reason=reason,
            analysis=analysis, state=state,
        )


class ReplyProvider(Protocol):
    async def reply(self, context: ConversationContext) -> ReplyDecision: ...


class HumanOnlyReplyProvider:
    """Default provider: observe and route to a human, never auto-answer."""

    async def reply(self, context: ConversationContext) -> ReplyDecision:
        return ReplyDecision.human("no_reply_provider_configured")


class StaticReplyProvider:
    """Credential-free provider used for wiring and dry runs.

    It sends one fixed acknowledgement per conversation and hands everything
    else to a human. It never states a business fact.
    """

    def __init__(self, text: str, *, once_per_conversation: bool = True):
        self.text = text.strip()
        self.once_per_conversation = bool(once_per_conversation)
        self._answered: set[str] = set()

    async def reply(self, context: ConversationContext) -> ReplyDecision:
        if not self.text:
            return ReplyDecision.human("static_reply_not_configured")
        if self.once_per_conversation and context.conversation_key in self._answered:
            return ReplyDecision.human("static_reply_already_sent")
        self._answered.add(context.conversation_key)
        return ReplyDecision(text=self.text, should_send=True, reason="static_reply", model="static")


class PipelineReplyProvider:
    """Adapter over the existing analyse -> route -> generate -> validate chain.

    Business facts may only come from a verified tool result; anything the
    validator rejects is escalated instead of sent.
    """

    def __init__(
        self,
        *,
        analyzer,
        router,
        state_manager,
        reply_generator,
        reply_validator,
        business_tools=None,
        strong_reply_generator=None,
        product_lookup=None,
        conversation_store=None,
        summary_service=None,
        recent_message_limit: int = 12,
        history_message_limit: int = 100,
        unknown_intent_limit: int = 2,
        metrics=None,
        enabled_media_kinds=(),
        media_cache=None,
        catalogue=None,
        unsupported_message_reply=None,
        shop_policy=None,
        order_handoff=True,
    ):
        self.analyzer = analyzer
        self.router = router
        self.state_manager = state_manager
        self.reply_generator = reply_generator
        self.reply_validator = reply_validator
        self.business_tools = business_tools
        self.strong_reply_generator = strong_reply_generator
        # `products.lookup_enabled`: with it off, product questions never reach
        # the catalogue and are answered from the conversation instead.
        self.product_lookup = product_lookup or ProductLookupPolicy(True)
        # The shop's product repository (`get_by_id`), used to notice that a
        # product this conversation was told about has since been deleted or
        # taken off sale - see _forget_removed_products.
        self.catalogue = catalogue
        # `messaging.unsupported_message_reply`: sent when a customer's message
        # is one TikTok Web cannot show (a voice message...), before a person
        # takes over. None = the reply language's own line.
        self.unsupported_message_reply = unsupported_message_reply
        # `products.shipping`: delivery facts added to every reply's verified
        # data (agent/shop_policy.py). Reassigned live by control/settings.py.
        self.shop_policy = shop_policy or ShopPolicy()
        # `messaging.order_handoff`: once a customer ordering has given both a
        # phone number and an address, the reply goes out and a person takes
        # over to create the order - the agent cannot create one itself.
        self.order_handoff = bool(order_handoff)
        # Without a store the provider still runs, entirely from what the open
        # thread shows - that is the credential-free/dry-run path.
        self.conversation_store = conversation_store
        # The store is bound to one account, so everything this provider loads
        # for the model - history, state, customer facts - is already that
        # account's data only. The id is kept for stamping and for logs.
        self.account_id = getattr(conversation_store, "account_id", "") or ""
        # Long conversations are summarised so the prompt stops growing with
        # them. This used to run only on the asynchronous path, so the flow that
        # actually answers customers never summarised anything.
        self.summary_service = summary_service
        self.recent_message_limit = max(1, int(recent_message_limit))
        self.history_message_limit = max(1, int(history_message_limit))
        # A customer the analyzer cannot understand twice in a row is a customer
        # a person should read. Same reason as above: this guard existed only on
        # the path that is not running.
        self.unknown_intent_limit = max(1, int(unknown_intent_limit))
        self.metrics = metrics
        self.enabled_media_kinds = frozenset(enabled_media_kinds)
        # Downloads a thread's pictures onto this server's disk before they
        # are persisted, because TikTok's own URLs are not something the
        # dashboard can display from - see services/media_cache.py. Optional:
        # without it the rows keep only `src` and the dashboard shows what it
        # can while those URLs last.
        self.media_cache = media_cache
        # Keyed by account *and* conversation: a bare conversation key would be
        # a shared name across accounts the moment one process ever served two.
        # Bounded: oldest entry out, because these are a fallback for running
        # without a database, not a second source of truth beside one.
        self._states: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._last_outbound: OrderedDict[tuple[str, str], str] = OrderedDict()

    def _key(self, context: ConversationContext) -> tuple[str, str]:
        return (self.account_id, context.conversation_key)

    @staticmethod
    def _remember(cache: OrderedDict, key, value) -> None:
        """Newest in, oldest out once the cache is full."""
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > MEMORY_CACHE_LIMIT:
            cache.popitem(last=False)

    def _event(self, context: ConversationContext) -> MessageEvent:
        return MessageEvent(
            account_id=self.account_id,
            platform=context.platform,
            conversation_id=context.conversation_key,
            customer_id=self._customer_id(context),
            customer_name=context.customer_name,
            message_id=context.message_id,
            message=context.message_text,
            metadata={"media": list(context.inbound_media)},
            timestamp=datetime.now(timezone.utc),
        )

    async def _recent(self, context: ConversationContext, event: MessageEvent):
        """The conversation so far, from the open thread or from the store.

        A caller reading a live thread supplies it; one that only has a message
        event (no browser in hand) supplies nothing, and the analyzer would then
        judge the message with no context at all.
        """
        rendered = self._history(context, event)
        if rendered or self.conversation_store is None:
            return rendered
        return await self.conversation_store.recent(
            context.conversation_key, self.recent_message_limit
        )

    def _history(self, context: ConversationContext, event: MessageEvent) -> list[ConversationMessage]:
        messages: list[ConversationMessage] = []
        for item in context.history[-self.recent_message_limit:]:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            direction = (
                MessageDirection.OUTBOUND
                if item.get("direction") == "OUTBOUND"
                else MessageDirection.INBOUND
            )
            messages.append(
                ConversationMessage(
                    message_id=str(item.get("id") or f"{event.conversation_id}-{len(messages)}"),
                    conversation_id=event.conversation_id,
                    customer_id=event.customer_id,
                    direction=direction,
                    text=text,
                )
            )
        return messages

    # state.metadata keys. Products the catalogue confirmed in this
    # conversation ({id: name}), and those since deleted or taken off sale.
    VERIFIED_PRODUCTS = "verified_products"
    REMOVED_PRODUCTS = "removed_products"

    # ReplyValidator refusals that are a wording mistake the model can fix,
    # with what the one rewrite is told. Anything not listed - a human review,
    # an unverified tool, a duplicate, the wrong language (already rewritten
    # once by the generator) - goes to a person as before.
    REVISABLE_REJECTIONS = {
        "empty_reply": "The previous draft was empty. Write the reply.",
        "reply_too_long": "The previous draft was too long. Write a much shorter reply.",
        "unverified_price_claim": (
            "The previous draft stated a price or amount that is not in Verified tool "
            "result. Rewrite it with no amount except those found there."
        ),
        "claims_missing_attachment": (
            "The previous draft said a photo or video is attached, or wrote a link or "
            "file path. Rewrite it without any of that."
        ),
        "denies_media_capability": (
            "The previous draft said the shop cannot send images or videos. That is "
            "false. Rewrite it without saying so."
        ),
        "mentions_removed_product": (
            "The previous draft described a product the shop no longer sells. Only say "
            "it is no longer available; never describe, recommend or price it."
        ),
    }

    # How many products a "which one did you mean?" reply names at most.
    MAX_LISTED_PRODUCTS = 10
    PRODUCT_TOOLS = frozenset({"get_product", "check_inventory"})

    def _remember_verified_product(self, state, tool_result) -> bool:
        """Note the product a successful catalogue lookup confirmed."""
        if (
            tool_result is None or not tool_result.success
            or tool_result.tool_name not in self.PRODUCT_TOOLS
        ):
            return False
        data = tool_result.data or {}
        product_id = str(data.get("id") or data.get("product_id") or "").strip()
        if not product_id:
            return False
        name = str(data.get("name") or data.get("product_name") or product_id).strip()
        verified = dict(state.metadata.get(self.VERIFIED_PRODUCTS) or {})
        removed = dict(state.metadata.get(self.REMOVED_PRODUCTS) or {})
        if verified.get(product_id) == name and product_id not in removed:
            return False
        verified[product_id] = name
        removed.pop(product_id, None)
        state.metadata = {
            **state.metadata,
            self.VERIFIED_PRODUCTS: verified,
            self.REMOVED_PRODUCTS: removed,
        }
        return True

    def _forget_removed_products(self, state) -> list[str]:
        """Products this conversation discussed that the shop no longer sells.

        Reported by testing: a product deleted from the catalogue was still
        described and recommended, because the conversation remembered it -
        `state.product_id/product_name` carry over from turn to turn, and
        earlier replies describing it are in the history the model reads. A
        product lookup for it fails and goes to a person, but a question that
        needs no lookup ("tư vấn thêm về áo đó") was answered from memory.

        Only products the catalogue itself confirmed earlier are checked, so a
        vague "áo đó" is never mistaken for a deleted product. Found ones move
        to the removed list (kept, so later turns are told too, until the
        product comes back), stop being the conversation's current product,
        and are named to the model and the validator.
        """
        if self.catalogue is None:
            return []
        verified = dict(state.metadata.get(self.VERIFIED_PRODUCTS) or {})
        removed = dict(state.metadata.get(self.REMOVED_PRODUCTS) or {})
        changed = False
        for product_id in list(verified) + list(removed):
            try:
                product = self.catalogue.get_by_id(product_id)
            except Exception:
                logger.exception("[reply] catalogue check failed product=%s", product_id)
                continue
            gone = product is None or product.get("is_active") is False
            if gone and product_id in verified:
                removed[product_id] = verified.pop(product_id)
                changed = True
            elif not gone and product_id in removed:
                verified[product_id] = removed.pop(product_id)
                changed = True
        if changed:
            state.metadata = {
                **state.metadata,
                self.VERIFIED_PRODUCTS: verified,
                self.REMOVED_PRODUCTS: removed,
            }
        folded = {name.casefold() for name in removed.values()}
        if state.product_id in removed or (state.product_name or "").casefold() in folded:
            state.product_id = None
            state.product_name = None
        return sorted(set(removed.values()))

    async def _run_tool(self, analysis):
        if not self.business_tools or not analysis.tool_name:
            return None
        try:
            result = await self.business_tools.execute(
                analysis.tool_name, analysis.tool_args
            )
            if not result.success:
                # Argument values may contain customer/order data. Names plus
                # the service's safe error are enough to diagnose a schema
                # mismatch without leaking those values into the log.
                logger.warning(
                    "[reply] business tool rejected tool=%s arg_keys=%s error=%s",
                    analysis.tool_name,
                    sorted((analysis.tool_args or {}).keys()),
                    result.error or "unknown",
                )
            return result
        except Exception:
            logger.exception("[reply] business tool failed tool=%s", analysis.tool_name)
            return None

    @staticmethod
    def _verified_attachments(analysis, tool_result) -> tuple[Attachment, ...]:
        """Select one verified catalogue file for every explicitly requested kind."""
        requested = tuple(getattr(analysis, "requested_media", ()) or ())
        if not requested or tool_result is None or not tool_result.success:
            return ()
        media = (tool_result.data or {}).get("media") or {}
        selected = []
        for kind in requested:
            entries = media.get("images" if kind == "image" else "videos") or ()
            for item in entries:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path and item.get("exists") is True and item.get("valid_type") is True:
                    selected.append(Attachment(
                        kind=kind,
                        path=path,
                        name=str(item.get("name") or ""),
                    ))
                    break
        return tuple(selected)

    async def _load_state(self, context: ConversationContext, event: MessageEvent):
        if self.conversation_store is not None:
            stored = await self.conversation_store.get_state(context.conversation_key)
            if stored is not None:
                return stored
        return (
            self._states.get(self._key(context))
            or self.state_manager.new(event)
        )

    async def _save_state(self, context: ConversationContext, state) -> None:
        self._remember(self._states, self._key(context), state)
        if self.conversation_store is not None:
            await self.conversation_store.save_state(state)

    @staticmethod
    def _customer_id(context: ConversationContext) -> str:
        """Identity comes from the conversation, never from the header text.

        The chat header renders the display name (`manh1`) or the handle
        (`@manh14760`) depending on which selector TikTok happens to expose, so
        keying on it split one customer's state and history across several ids.
        `customer_name` stays for display only.
        """
        return context.conversation_key or (context.customer_name or "")

    async def _store_messages(
        self, context: ConversationContext, messages, *, source: str = "dom",
    ) -> None:
        """Persist rendered messages, identified by content.

        `store_visible_messages` hashes each message and inserts with
        `ON CONFLICT DO NOTHING`, so the same message stored twice - once when it is
        sent, once when a later scan sees it in the thread - stays one row.
        """
        repository = getattr(self.conversation_store, "context_repository", None)
        if repository is None or not messages:
            return
        messages = await self._localize_media(messages)
        try:
            await repository.store_visible_messages(
                context.conversation_key,
                messages,
                customer_id=self._customer_id(context),
                source=source,
                platform=context.platform,
                username=context.customer_name,
                display_name=context.customer_name,
                avatar_url=context.avatar_url,
            )
        except Exception:
            # Losing history is not a reason to leave a customer unanswered.
            logger.exception(
                "[reply] storing messages failed conversation=%s",
                context.conversation_key,
            )

    async def _localize_media(self, messages):
        """Swap every remote picture URL for a file on this server's disk.

        Done before the rows are written, so the `path` the dashboard needs is
        part of the message from its first version onwards rather than
        something a later pass has to go back and add.
        """
        if self.media_cache is None:
            return messages
        resolved = []
        for message in messages:
            media = getattr(message, "media", None) or []
            if not media:
                resolved.append(message)
                continue
            localized = await self.media_cache.localize(media)
            try:
                message.media = localized
            except (AttributeError, TypeError):
                # A frozen message object: keep the fields
                # `store_visible_messages` reads and leave the original alone.
                message = SimpleNamespace(
                    text=getattr(message, "text", ""),
                    direction=getattr(message, "direction", ""),
                    order=getattr(message, "order", None),
                    media=localized,
                )
            resolved.append(message)
        return resolved

    async def _persist_thread(self, context: ConversationContext) -> None:
        """Store the thread as currently rendered, so later replies have history."""
        await self._store_messages(context, [
            SimpleNamespace(
                text=item.get("text") or "", direction=item.get("direction"),
                # DOM position within this render - see
                # ConversationContextRepository.message_hash's docstring for
                # why this rides along: without it, a customer repeating the
                # exact same text later in the conversation ("Bạn ơi", "dạ",
                # ...) hashed identically to the earlier occurrence and was
                # silently dropped as a duplicate instead of stored.
                order=item.get("index"),
                # Images/videos the DOM parser found in this bubble (see
                # extension/platforms/tiktok.js's messageMedia) - each item is
                # {kind, src, width, height}, "src" being the real CDN URL the
                # browser itself rendered, so the dashboard can embed it as-is.
                media=item.get("media") or [],
            )
            for item in context.history
        ])

    async def persist_thread(self, context: ConversationContext) -> None:
        """Public entry point for syncing the rendered thread without
        generating a reply - used when a human owns the conversation, so
        the agent keeps writing down what it observes without ever calling
        the model."""
        await self._persist_thread(context)

    async def record_sent(
        self, context: ConversationContext, text: str, attachments=(), *,
        verified: bool = True,
    ) -> None:
        """Record a reply that was verified as delivered.

        Without this the reply is only written down when a later scan happens
        to re-read the thread, so the last answer of a conversation the
        customer never continues would never be stored at all.

        `attachments` (`models.outbound_message.Attachment`) is only ever
        product-catalogue files (see `_verified_attachments` above) - `path`
        is an absolute filesystem path, not a URL, so the dashboard serves it
        through its own endpoint scoped to the account's product root
        (`dashboard_api/main.py`'s `read_conversation_media`), unlike inbound
        media whose `src` is already a public CDN URL.

        `verified=False` stores the row with source "unverified" - a reply on
        screen whose place after the inbound was not proven. The next DOM
        re-read of the thread re-keys it to an ordinary row.
        """
        answer = str(text or "").strip()
        media = [
            {"kind": item.kind, "path": item.path, "name": item.name}
            for item in attachments
        ]
        if not answer and not media:
            return
        self._remember(self._last_outbound, self._key(context), answer)
        await self._store_messages(context, [
            SimpleNamespace(
                text=answer, direction=MessageDirection.OUTBOUND.value, media=media,
            )
        ], source="dom" if verified else "unverified")

    async def _customer_context(self, context: ConversationContext) -> dict:
        if self.conversation_store is None:
            return {}
        return await self.conversation_store.context(
            context.conversation_key, self.recent_message_limit
        )

    @staticmethod
    def _attach_verified_customer_context(analysis, customer_context, recent_messages):
        """Stamp persistence/DOM context after any analyzer implementation.

        Production uses `MessageAnalyzer`, but tests and custom deployments may
        supply another analyzer.  Verified CRM data must still come from the
        account-scoped store and rendered thread, never from model output.
        """
        # Credential-free/custom analyzers may already carry explicitly
        # supplied verified context. With no store and no rendered history,
        # there is no stronger evidence here with which to replace it.
        if not customer_context and not recent_messages:
            return analysis
        analysis.verified_customer_context = {
            "customer_profile": customer_context.get("customer_profile", {}),
            "customer_facts": customer_context.get("customer_facts", {}),
            "recent_messages": [
                {
                    "direction": str(getattr(message, "direction", "")),
                    "text": str(getattr(message, "text", "")),
                }
                for message in recent_messages
            ],
        }
        return analysis

    async def _last_outbound_text(self, context: ConversationContext):
        if self.conversation_store is not None:
            return await self.conversation_store.last_outbound_text(
                context.conversation_key
            )
        return self._last_outbound.get(self._key(context))

    async def reply(self, context: ConversationContext) -> ReplyDecision:
        event = self._event(context)
        media_only_inbound = bool(context.inbound_media) and not str(
            context.message_text or ""
        ).strip()
        timer = context.timer
        with timer.phase("llm_prepare"):
            state = await self._load_state(context, event)
            history = await self._recent(context, event)
            await self._persist_thread(context)
        if self._has_unsupported_message(context):
            # A voice message (or another type only the app shows) reaches us
            # as TikTok's placeholder sentence. The model must not answer that
            # sentence as if the customer typed it, and nobody here can hear
            # the message: a fixed line goes out and a person takes over.
            self._metric("message_unsupported_type", platform=context.platform)
            return ReplyDecision(
                text=unsupported_message_ack(
                    self.reply_language, self.unsupported_message_reply,
                ),
                should_send=True,
                requires_human=True,
                reason=UNSUPPORTED_MESSAGE_REASON,
                model="unsupported-message-ack",
                state=state,
            )
        with timer.phase("llm_prepare"):
            customer_context = await self._customer_context(context)

        with timer.phase("llm_analyze"):
            analysis = await self.analyzer.analyze(event, state, history, customer_context)
            analysis = self._attach_verified_customer_context(
                analysis, customer_context, history
            )
            if analysis.requires_history and self.conversation_store is not None:
                stored = await self.conversation_store.history(
                    context.conversation_key, self.history_message_limit
                )
                if stored:
                    analysis = await self.analyzer.analyze(
                        event, state, stored, customer_context
                    )
                    analysis = self._attach_verified_customer_context(
                        analysis, customer_context, stored
                    )
        if media_only_inbound:
            # Defence in depth for custom analyzers: an inbound attachment is
            # something to acknowledge, never evidence that the customer asked
            # us to fetch and send catalogue media.
            analysis = without_tool(analysis)
            analysis.requested_media = []
        # Whatever the customer stated about themselves is written down before
        # the reply is generated, so it survives a failed or withheld send.
        if self.conversation_store is not None:
            explicit_facts = dict(
                getattr(analysis, "explicit_customer_facts", {}) or {}
            )
            model_facts = {
                key: value for key, value in analysis.customer_facts.items()
                if key not in explicit_facts
            }
            # Keep the original call even when there are no new facts: it also
            # registers the verified TikTok username/display name in the
            # customer profile.
            await self.conversation_store.merge_customer_facts(
                context.conversation_key,
                model_facts,
                event.message_id,
                analysis.confidence,
                username=context.customer_name,
                display_name=context.customer_name,
                customer_id=self._customer_id(context),
                platform=context.platform,
            )
            if explicit_facts:
                # Regex-backed facts came verbatim from this inbound message;
                # their storage confidence is independent of how confidently
                # Gemini classified the customer's intent.
                await self.conversation_store.merge_customer_facts(
                    context.conversation_key,
                    explicit_facts,
                    event.message_id,
                    1.0,
                    username=context.customer_name,
                    display_name=context.customer_name,
                    customer_id=self._customer_id(context),
                    platform=context.platform,
                )
        analysis = self.product_lookup.apply(analysis)
        if media_only_inbound:
            # ProductLookupPolicy can infer get_product from intent/entities.
            # Re-apply the boundary after it so no tool or attachment can be
            # resurrected from conversation history or model inference.
            analysis = without_tool(analysis)
            analysis.requested_media = []
        # A tool the registry cannot execute is a naming mistake by the model,
        # not a customer problem: the request is dropped so the message is
        # answered on the ordinary LLM path instead of waiting for a human.
        if analysis.requires_tool and not tool_is_available(
            self.business_tools, analysis.tool_name
        ):
            logger.warning(
                "[reply] unknown business tool dropped tool=%s conversation=%s",
                analysis.tool_name, context.conversation_key,
            )
            analysis = without_tool(analysis)
        state = self.state_manager.transition(state, event, analysis)
        removed_products = self._forget_removed_products(state)
        # A customer whose intent the analyzer misses repeatedly is not going to
        # be served by another guess. `unknown_intent_count` is reset by any
        # message that does parse, so this only fires on a real run of them.
        if state.unknown_intent_count >= self.unknown_intent_limit:
            analysis.requires_human = True
            analysis.next_stage = "human_required"
            # `require_human` only stamps stage/requires_human - a second
            # `transition()` call here would recompute `unknown_intent_count`
            # off the already-incremented state and double-count it.
            state = self.state_manager.require_human(state)
        await self._save_state(context, state)

        route = self.router.route(analysis)
        if route is MessageRoute.HUMAN:
            return self._human(
                f"routed_to_human:{analysis.intent}", context, analysis, state
            )

        with timer.phase("llm_tool"):
            tool_result = await self._run_tool(analysis) if route is MessageRoute.TOOL else None
        if self._remember_verified_product(state, tool_result):
            await self._save_state(context, state)
        notes: list[str] = []
        if route is MessageRoute.TOOL and (tool_result is None or not tool_result.success):
            # A name the catalogue does not know is a question to ask the
            # customer, not a person's job; anything else (the lookup itself
            # failing, a size the product has no data for) still is.
            tool_result = self._product_not_found(analysis)
            if tool_result is None:
                return self._human("required_tool_unavailable", context, analysis, state)
            analysis.requested_media = []
            notes.append(
                "The product the customer asked about is not in the shop's catalogue. "
                "Say the shop could not find it, name the products in Verified tool "
                "result's products_on_sale, and ask which one they mean. State no "
                "price, stock or other product fact."
            )

        requested_media = tuple(analysis.requested_media)
        enabled_requests = all(
            kind in self.enabled_media_kinds for kind in requested_media
        )
        attachments = (
            self._verified_attachments(analysis, tool_result)
            if enabled_requests else ()
        )
        attached_kinds = {attachment.kind for attachment in attachments}
        missing_kinds = [
            kind for kind in requested_media if kind not in attached_kinds
        ]
        if missing_kinds:
            note = self._missing_media_note(missing_kinds, route, tool_result)
            if not enabled_requests or note is None:
                return self._human(
                    f"requested_{'_and_'.join(missing_kinds)}_unavailable",
                    context, analysis, state,
                )
            notes.append(note)

        # Attachments were chosen from the tool's own result above; the shop's
        # delivery facts join it only for the generator and the validator.
        tool_result = self._with_shop_policy(tool_result)
        generator = (
            self.strong_reply_generator
            if route is MessageRoute.STRONG_LLM and self.strong_reply_generator
            else self.reply_generator
        )
        with timer.phase("llm_generate"):
            result = await generator.generate(
                event, state, analysis, tool_result, attachments=attachments,
                unavailable_products=removed_products,
                # Only when there is one, so a generator without the parameter
                # keeps working for every ordinary reply.
                **({"notes": tuple(notes)} if notes else {}),
            )
        with timer.phase("llm_validate"):
            last_outbound = await self._last_outbound_text(context)
            validation = self.reply_validator.validate(
                result, analysis, tool_result, last_outbound, attachments=attachments,
                removed_products=removed_products, reply_language=self.reply_language,
            )
        if not validation.allowed and validation.reason == "asks_for_known_address":
            # The address has already been verified from this account-scoped
            # conversation/store. Asking for it again is a generation mistake,
            # not a reason to leave the customer unanswered. Use a narrow,
            # fact-free acknowledgement instead of making another model call.
            result = ReplyResult(
                text=address_ack(self.reply_language),
                should_send=True,
                model="verified-context-fallback",
            )
            validation = self.reply_validator.validate(
                result, analysis, tool_result, last_outbound, attachments=attachments,
                removed_products=removed_products, reply_language=self.reply_language,
            )
            if validation.allowed:
                logger.info(
                    "[reply] replaced repeated address request with verified-context "
                    "acknowledgement conversation=%s",
                    context.conversation_key,
                )
        revision = self.REVISABLE_REJECTIONS.get(validation.reason or "")
        if not validation.allowed and revision is not None:
            # A draft that broke one fixable rule gets one rewrite told what
            # was wrong. The rewrite faces the same validator, so this only
            # changes who fixes the wording - the model, before a person.
            logger.info(
                "[reply] draft refused reason=%s, rewriting once conversation=%s",
                validation.reason, context.conversation_key,
            )
            with timer.phase("llm_generate"):
                result = await generator.generate(
                    event, state, analysis, tool_result, attachments=attachments,
                    unavailable_products=removed_products,
                    notes=(*notes, revision),
                )
            with timer.phase("llm_validate"):
                validation = self.reply_validator.validate(
                    result, analysis, tool_result, last_outbound, attachments=attachments,
                    removed_products=removed_products, reply_language=self.reply_language,
                )
        if not validation.allowed:
            if validation.reason == "duplicate_reply":
                # Deduplication is not a handoff. Sending the generic handoff
                # acknowledgement here would merely replace one duplicate with
                # another message and could repeat on every inbox scan.
                return ReplyDecision(
                    should_send=False,
                    requires_human=False,
                    reason="duplicate_reply",
                    analysis=analysis,
                    state=state,
                )
            return self._human(
                validation.reason or "reply_rejected", context, analysis, state
            )

        order_ready = self._order_ready(analysis, state, customer_context)
        if order_ready:
            await self._save_state(context, state)
            self._metric("message_order_ready", platform=context.platform)
        self._remember(self._last_outbound, self._key(context), result.text)
        with timer.phase("llm_summarize"):
            await self._maybe_summarize(context, state)
        self._metric("message_reply_generated", platform=context.platform)
        return ReplyDecision(
            # The reply still goes out; the watcher then hands the
            # conversation to a person, who creates the order.
            text=result.text, should_send=True, requires_human=order_ready,
            reason=ORDER_READY_REASON if order_ready else "generated",
            model=result.model, attachments=attachments,
            analysis=analysis, state=state,
            intent=str(getattr(analysis, "intent", "") or ""),
            conversation_stage=str(getattr(state, "stage", "") or ""),
            product_ids=tuple(dict.fromkeys(
                str(value).strip() for value in (
                    (analysis.tool_args or {}).get("product_id"),
                    (analysis.tool_args or {}).get("product_code"),
                ) if value
            )),
            source_facts=(
                (dict(tool_result.data or {}),)
                if tool_result is not None and tool_result.success else ()
            ),
            inbound_watermark="|".join(
                str(item.get("id") or "") for item in context.pending_messages
            ) or context.message_id,
        )

    # The intents/stages that mean "this customer is placing an order".
    ORDERING_INTENTS = frozenset({"ask_order"})
    ORDERING_STAGES = frozenset({"ordering"})

    def _order_ready(self, analysis, state, customer_context) -> bool:
        """Ordering, with a phone number and an address known - once per pair.

        The facts are the stored ones plus whatever this message just stated
        (merged into the store before generation, but not into the context
        that was loaded first). Quantity is left to the person who confirms
        the order: a customer who never states one must still reach them.
        Records the pair in the state metadata so the same order is handed
        over once, not on every following message.
        """
        if not self.order_handoff:
            return False
        ordering = (
            str(getattr(analysis, "intent", "") or "") in self.ORDERING_INTENTS
            or str(getattr(analysis, "next_stage", "") or "") in self.ORDERING_STAGES
            or str(getattr(state, "stage", "") or "") in self.ORDERING_STAGES
        )
        if not ordering:
            return False
        facts = {
            key: str((value or {}).get("value") if isinstance(value, dict) else value or "")
            for key, value in ((customer_context or {}).get("customer_facts") or {}).items()
        }
        facts.update({key: str(value) for key, value in (analysis.customer_facts or {}).items()})
        facts.update(dict(getattr(analysis, "explicit_customer_facts", {}) or {}))
        phone = " ".join(facts.get("phone", "").split())
        address = " ".join(facts.get("address", "").split())
        if not phone or not address:
            return False
        signature = f"{phone}|{address}".casefold()
        metadata = getattr(state, "metadata", None)
        if metadata is None:
            return True
        if metadata.get(self.ORDER_HANDOFF_KEY) == signature:
            return False
        metadata[self.ORDER_HANDOFF_KEY] = signature
        return True

    ORDER_HANDOFF_KEY = "order_handoff_signature"

    def _with_shop_policy(self, tool_result):
        """`tool_result` plus the shop's delivery facts under `shop_policy`."""
        facts = self.shop_policy.facts()
        if not facts:
            return tool_result
        if tool_result is None:
            return ToolResult(tool_name="shop_policy", success=True,
                              data={"shop_policy": facts})
        return tool_result.model_copy(
            update={"data": {**(tool_result.data or {}), "shop_policy": facts}}
        )

    @staticmethod
    def _has_unsupported_message(context: ConversationContext) -> bool:
        """Any message of this unanswered burst is a placeholder for a type
        TikTok Web cannot show."""
        texts = [str(item.get("text") or "") for item in context.pending_messages or ()]
        if not texts:
            texts = [str(context.message_text or "")]
        return any(is_unsupported_placeholder(text) for text in texts)

    @property
    def reply_language(self) -> str:
        """The language the two canned replies above are written in.

        Read off the generator instead of being stored here: control/settings.py's
        "messaging.reply_language" setter writes it there, so switching language
        in Dashboard > Kịch bản reaches these fallbacks on the next message with
        no second wiring path to keep in step. A runtime whose generator does not
        carry one answers in the default language, as it always did.
        """
        return normalize_language(getattr(self.reply_generator, "reply_language", None))

    def _product_not_found(self, analysis) -> ToolResult | None:
        """The shop's products on sale, when the one asked about does not exist.

        A failed product lookup means one of three things: the name matches
        nothing in the catalogue, the product exists but lacks the data asked
        for (a size with no variants), or the lookup itself broke. Only the
        first is the customer's to clarify, so this re-checks the catalogue and
        answers None - a person - for the other two, for a catalogue it cannot
        read, and for a shop with nothing on sale to offer instead.
        """
        catalogue = self.catalogue
        if (
            catalogue is None
            or analysis.tool_name not in self.product_lookup.tools
            or not all(
                hasattr(catalogue, name) for name in ("get_by_id", "resolve", "load_products")
            )
        ):
            return None
        reference = str((analysis.tool_args or {}).get("product_id") or "").strip()
        if not reference:
            return None
        try:
            if catalogue.get_by_id(reference) is not None or catalogue.resolve(reference) is not None:
                return None
            on_sale = [
                str(product.get("name") or "").strip()
                for product in catalogue.load_products()
                if product.get("is_active", True) is not False
            ]
        except Exception:
            logger.exception("[reply] re-checking the catalogue failed reference=%s", reference)
            return None
        on_sale = [name for name in on_sale if name][: self.MAX_LISTED_PRODUCTS]
        if not on_sale:
            return None
        logger.info(
            "[reply] product not in catalogue, asking the customer which one products=%s",
            len(on_sale),
        )
        return ToolResult(
            tool_name=analysis.tool_name, success=True,
            data={"product_not_found": reference, "products_on_sale": on_sale},
        )

    def _missing_media_note(self, missing_kinds, route, tool_result) -> str | None:
        """What a reply says when a requested photo/video cannot go out, or None.

        Two cases are the customer's answer rather than a person's: the
        verified product simply has no such file (saying so is true), and no
        product was named (ask which). Anything else still goes to a person.
        """
        named = " or ".join({"image": "photo", "video": "video"}.get(k, k) for k in missing_kinds)
        if (
            tool_result is not None and tool_result.success
            and tool_result.tool_name in self.product_lookup.tools
            and "product_not_found" not in (tool_result.data or {})
        ):
            return (
                f"The customer asked for a {named} of the product, but the shop has none "
                f"on file for it. Say the shop does not have a {named} of this product yet "
                "- never that the shop cannot send one - and answer from Verified tool "
                "result."
            )
        if tool_result is None and route is not MessageRoute.TOOL and self.product_lookup.enabled:
            return (
                f"The customer asked for a {named} but did not say of which product. Ask "
                "which product they mean; do not say anything is attached."
            )
        return None

    @staticmethod
    def _knows_phone(analysis) -> bool:
        """A phone number for this customer: stored profile or facts, or
        stated in the message being answered."""
        verified = getattr(analysis, "verified_customer_context", None) or {}
        stored = (verified.get("customer_facts") or {}).get("phone")
        candidates = (
            (verified.get("customer_profile") or {}).get("phone"),
            stored.get("value") if isinstance(stored, dict) else stored,
            (getattr(analysis, "customer_facts", None) or {}).get("phone"),
            dict(getattr(analysis, "explicit_customer_facts", {}) or {}).get("phone"),
        )
        return any(str(value or "").strip() for value in candidates)

    def _human(self, reason, context, analysis, state) -> ReplyDecision:
        """Acknowledge safely while the operator receives the real escalation."""
        self._metric("message_requires_human", platform=context.platform, reason=reason)
        return ReplyDecision(
            # Asking for a phone number the shop already has would read as not
            # listening; asking when it has none gives the person taking over
            # a way to reach the customer.
            text=handoff_ack(self.reply_language, ask_contact=not self._knows_phone(analysis)),
            should_send=True,
            requires_human=True,
            reason=reason,
            model="human-handoff-ack",
            analysis=analysis,
            state=state,
        )

    async def _maybe_summarize(self, context: ConversationContext, state) -> None:
        """Compress a long conversation so the next prompt stays bounded."""
        if (
            self.summary_service is None
            or self.conversation_store is None
            or not self.summary_service.should_summarize(state)
        ):
            return
        try:
            messages = await self.conversation_store.recent(
                context.conversation_key, self.summary_service.source_limit
            )
            summary = await self.summary_service.summarize(state, messages)
            await self._save_state(
                context, self.state_manager.update_summary(state, summary)
            )
        except Exception as exc:
            # A missing summary costs prompt size, never an answer.
            logger.warning(
                "[reply] conversation summary failed conversation=%s error_type=%s",
                context.conversation_key, type(exc).__name__, exc_info=True,
            )

    def _metric(self, name, **labels):
        if self.metrics is not None:
            self.metrics.increment(name, **labels)
