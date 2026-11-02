from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


DEFAULT_MESSAGE_INTENTS = frozenset({
    "greeting", "faq", "ask_product", "ask_price", "check_inventory",
    "ask_shipping", "ask_order", "order_status", "complaint",
    "return_request", "product_feedback", "thank_you", "unknown",
})

ALLOWED_CUSTOMER_FACTS = frozenset({
    "name", "phone", "email", "address", "city", "product_interest",
    "size", "color", "quantity", "purchase_note",
})


class MessageEntities(BaseModel):
    product_id: str | None = None
    product_name: str | None = None
    size: str | None = None
    quantity: int | None = Field(default=None, ge=1)


# A product asset the customer explicitly asked to be sent.
MediaKind = Literal["image", "video"]


class MessageAnalysis(BaseModel):
    # Populated by deterministic extraction after the LLM response has been
    # validated. Keeping this private means Gemini cannot claim that a contact
    # value was explicitly present when it was not in the new inbound message.
    _explicit_customer_facts: dict[str, str] = PrivateAttr(default_factory=dict)

    intent: str
    sentiment: str = "neutral"
    next_stage: str
    entities: MessageEntities = Field(default_factory=MessageEntities)
    requires_history: bool = False
    requires_tool: bool = False
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    requires_human: bool = False
    can_reply_directly: bool = False
    requires_strong_llm: bool = False
    # The model may identify the customer's request, but it never chooses a
    # file. A verified catalogue result is the only source of an attachment.
    requested_media: list[MediaKind] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    customer_facts: dict[str, str] = Field(default_factory=dict)
    verified_customer_context: dict[str, Any] = Field(default_factory=dict)

    @property
    def explicit_customer_facts(self) -> dict[str, str]:
        return dict(self._explicit_customer_facts)

    def set_explicit_customer_facts(self, facts: dict[str, str]) -> None:
        self._explicit_customer_facts = {
            key: str(value).strip()
            for key, value in dict(facts or {}).items()
            if key in ALLOWED_CUSTOMER_FACTS and str(value).strip()
        }

    @field_validator("requested_media", mode="before")
    @classmethod
    def normalize_requested_media(cls, value):
        """Accept old persisted scalar analyses while emitting the new list form."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def tool_request_is_complete(self):
        if self.requires_tool and not self.tool_name:
            raise ValueError("tool_name is required when requires_tool is true")
        if not self.requires_tool:
            self.tool_name = None
            self.tool_args = {}
        self.customer_facts = {
            key: str(value).strip()
            for key, value in self.customer_facts.items()
            if key in ALLOWED_CUSTOMER_FACTS and str(value).strip()
        }
        return self
