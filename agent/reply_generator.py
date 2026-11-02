import json

from agent.product_lookup import ProductLookupPolicy
from agent.reply_language import (
    default_reply_style,
    is_wrong_language,
    language_instruction,
    language_name,
    normalize_language,
)
from agent.reply_validator import denies_media_capability
from agent.scenario_policy import ScenarioPolicy
from llm.base import LLMProvider
from models.analysis import MessageAnalysis
from models.customer_state import CustomerState
from models.message_event import MessageEvent
from models.message_errors import MessageErrorType, MessageProcessingError
from models.reply import ReplyResult
from models.tool import ToolResult


# Dashboard > Kịch bản edits `messaging.reply_style`; these bound what one
# save can inject into every reply prompt.
MAX_STYLE_RULES = 20
MAX_STYLE_RULE_CHARACTERS = 300


def render_reply_style(rules) -> str:
    """The style block for the prompt, or "" when every rule is blank."""
    cleaned = [" ".join(str(rule).split()) for rule in (rules or ()) if str(rule).strip()]
    if not cleaned:
        return ""
    return "Style rules (mandatory):\n" + "".join(f"- {rule}\n" for rule in cleaned)


class ReplyGenerator:
    def __init__(
        self, llm: LLMProvider, max_characters=1000, product_lookup=None,
        scenario_policy=None, reply_style=None, reply_language=None,
    ):
        self.llm = llm
        self.max_characters = int(max_characters)
        # `messaging.reply_style` in config - tone/layout only; the fact rules
        # in `_rules` apply regardless of what the operator writes here.
        # None: the reply language's own default rules (agent/reply_language.py),
        # following the language when it changes; a list - even an empty one -
        # is the operator's choice. Reassigned live by control/settings.py's
        # "messaging.reply_style" setter.
        self.style_rules = None if reply_style is None else list(reply_style)
        # `messaging.reply_language`: the language every reply is written in,
        # whatever language the customer used. Reassigned live by
        # control/settings.py's "messaging.reply_language" setter, so it is read
        # on each generate() call rather than baked into a prompt at boot.
        self.reply_language = normalize_language(reply_language)
        # Shared with the reply provider, so the prompt follows the same switch
        # that decides whether the catalogue is queried at all.
        self.product_lookup = product_lookup or ProductLookupPolicy(True)
        # Shared with the strong-LLM fallback generator (see main.py), so an
        # operator edit through the dashboard reaches both prompts at once.
        self.scenario_policy = scenario_policy or ScenarioPolicy()

    @property
    def style_block(self) -> str:
        """The style rules as they go into the prompt, for the current language."""
        rules = (
            self.style_rules if self.style_rules is not None
            else default_reply_style(self.reply_language)
        )
        return render_reply_style(rules)

    def _rules(self, analysis: MessageAnalysis) -> str:
        """The fact rules for this reply.

        With the lookup off there is no verified tool result to quote from, so
        insisting on one would leave the model with nothing to say; it answers
        from the thread instead. Order, payment and refund facts stay off
        limits either way - those are never in the conversation.
        """
        if self.product_lookup.enabled or analysis.requires_tool:
            return (
                "Do not invent product, stock, price, shipping, order, customer, refund, "
                "payment or policy facts. Business facts may only come from "
                "verified_tool_result. "
            )
        return (
            "The product database is not being queried for this reply. Answer only from "
            "the conversation context, state and analysis below. Do not state a price, "
            "stock level, order, refund or payment fact that the conversation does not "
            "already contain; if the customer asks for one, say you will confirm it. "
        )

    async def generate(
        self,
        event: MessageEvent,
        state: CustomerState,
        analysis: MessageAnalysis,
        tool_result: ToolResult | None = None,
        attachments=(),
        unavailable_products=(),
        notes=(),
    ) -> ReplyResult:
        """`notes`: what the reply pipeline needs this particular reply to do -
        ask which product was meant, say a product has no photo, or fix what
        ReplyValidator refused in the previous draft."""
        verified = tool_result.model_dump(mode="json") if tool_result else None
        if verified and isinstance(verified.get("data"), dict):
            # `media` carries real on-disk paths, which are also the exact
            # material a model reaches for when it invents a plausible-looking
            # (but fake) public URL for "the image" instead of just describing
            # it in words. The verified attachment pipeline reads `media`
            # straight from `tool_result` itself (see `_verified_attachments`
            # in reply_service.py), never from this prompt, so the model never
            # needs to see it - removing it here cannot break attachments.
            verified = {
                **verified,
                "data": {k: v for k, v in verified["data"].items() if k != "media"},
            }
        inbound_media = list((event.metadata or {}).get("media") or ())
        media_instruction = (
            "The customer sent media without a text caption. Explicitly "
            "acknowledge receiving the image/video, ask what they want help "
            "with, and do not guess the product identity. "
            if inbound_media and not event.message.strip() else ""
        )
        # The attachments are chosen before this reply is written (see
        # reply_service), so the model is told exactly what goes out with its
        # text. Left to infer it from "never write a path / never claim an
        # attachment", it was seen telling a customer the shop cannot send
        # pictures - in the same message as the picture.
        kinds = sorted({str(getattr(item, "kind", "")) for item in attachments} - {""})
        if kinds:
            named = " and ".join({"image": "a product photo", "video": "a product video"}.get(k, k) for k in kinds)
            attachment_instruction = (
                f"This reply is sent together with {named} of the product, attached by the "
                "system. Say briefly, in one short sentence, that the shop is sending it - "
                f"in {language_name(self.reply_language)} like the rest of the reply, never "
                "copied from this instruction. Never say the shop cannot send images or "
                "videos. "
            )
        else:
            attachment_instruction = (
                "No image or video is sent with this reply: do not say one is attached. "
                "Never say the shop or the system cannot send images or videos - it can "
                "when a customer asks for one. "
            )
        # Products this conversation was told about that the shop has since
        # deleted or taken off sale (reply_service._forget_removed_products).
        removed_instruction = (
            "No longer sold by the shop: "
            f"{json.dumps(list(unavailable_products), ensure_ascii=False)}. If the customer "
            "asks about one of these, say it is no longer available; never describe, "
            "recommend or price it from earlier messages. "
            if unavailable_products else ""
        )
        notes_block = "".join(
            f"For this reply (mandatory): {note}\n" for note in notes if str(note).strip()
        )
        prompt = (
            "Write a brief customer-service reply. "
            f"{language_instruction(self.reply_language)}"
            "Return only the requested JSON schema. Do not claim an action was completed. "
            "Inspect Analysis.verified_customer_context before replying. Never ask the "
            "customer to repeat a name, phone number, email, address or city that is "
            "already present there; acknowledge it and continue with the current request. "
            "State product facts - price, stock, material, size, description, availability - "
            "only from Verified tool result below. Earlier messages and the conversation "
            "summary can be out of date: the product may have changed or been removed. "
            f"{removed_instruction}"
            "Attachments are sent by a separate verified UI step. Never write a local file "
            "path, URL, link, Markdown image, or bracketed image filename referring to a "
            "product photo or video - not even a plausible-looking one. "
            f"{attachment_instruction}"
            f"{media_instruction}"
            f"{self._rules(analysis)}"
            "Verified tool result's shop_policy, when present, holds the shop's "
            "delivery terms (shipping fee, free-shipping quantity, delivery time, "
            "cash on delivery, area); a total may be stated only as quantity x a "
            "verified price, plus the verified shipping fee when it applies. "
            f"Maximum {self.max_characters} characters.\n"
            f"{self.style_block}"
            f"{self.scenario_policy.render()}"
            f"{notes_block}"
            f"Message: {json.dumps(event.message, ensure_ascii=False)}\n"
            f"Inbound media: {json.dumps(inbound_media, ensure_ascii=False)}\n"
            f"State: {state.model_dump_json(exclude={'metadata', 'last_message'})}\n"
            f"Analysis: {analysis.model_dump_json()}\n"
            f"Verified tool result: {json.dumps(verified, ensure_ascii=False)}"
        )
        result = await self._generate_once(prompt)
        if is_wrong_language(self.reply_language, result.text):
            # One forced retry, never a silent send: a Vietnamese customer
            # answered in generic English is exactly the defect this guards
            # against. The retry always wins even if it still trips the same
            # check - the original draft is already known wrong, so it is never
            # a better fallback than the model's best remaining attempt.
            name = language_name(self.reply_language)
            forced = (
                f"{prompt}\nThe previous draft was not in {name}. "
                f"Rewrite the whole reply in {name}."
            )
            result = await self._generate_once(forced)
        if denies_media_capability(result.text):
            # Same one-retry rule as above. If the retry still says it, the
            # validator refuses the reply and the thread goes to a person.
            forced = (
                f"{prompt}\nThe previous draft told the customer the shop cannot send "
                "images or videos. That is false. Rewrite it without saying so."
            )
            result = await self._generate_once(forced)
        result.model = self.llm.model_name
        return result

    async def _generate_once(self, prompt: str) -> ReplyResult:
        try:
            return await self.llm.generate_structured(prompt, ReplyResult)
        except Exception as exc:
            raise MessageProcessingError(
                MessageErrorType.LLM_ERROR, "Reply generation failed"
            ) from exc
