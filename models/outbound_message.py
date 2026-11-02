from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class Attachment(BaseModel):
    """One file to send alongside (or instead of) text."""

    kind: Literal["image", "video"] = "image"
    path: str
    name: str = ""
    mime: str = ""


class OutboundMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # The account this reply is sent *as*. Carried so the outbox row, the send
    # task and the browser profile can all be checked against one another
    # before anything reaches a customer.
    account_id: str = ""
    platform: str
    conversation_id: str
    customer_id: str
    customer_name: str | None = None
    # Empty text is valid only for a message that carries attachments; an
    # image-only reply could not be constructed at all while this had
    # `min_length=1`.
    text: str = Field(default="", max_length=1000)
    attachments: list[Attachment] = Field(default_factory=list)
    source_event_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _must_carry_something(self) -> "OutboundMessage":
        if not self.text.strip() and not self.attachments:
            raise ValueError("an outbound message needs text or an attachment")
        return self
