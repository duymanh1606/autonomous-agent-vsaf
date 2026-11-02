import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, model_validator


class MessageEventType(str, Enum):
    RECEIVED = "RECEIVED"
    SENT = "SENT"
    DELIVERY_UPDATED = "DELIVERY_UPDATED"


class MessageDirection(str, Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    UNKNOWN = "UNKNOWN"


class MessageEventStatus(str, Enum):
    DETECTED = "DETECTED"
    NEEDS_REPLY = "NEEDS_REPLY"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    REPLIED = "REPLIED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class MessageEvent(BaseModel):
    """Platform-neutral message event emitted by a UI observer."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    # The account that saw this message. Empty means "not stamped yet"; the
    # repositories treat a stamped event as a hard claim and refuse it when it
    # names a different account than the store they are writing to.
    account_id: str = ""
    event_type: MessageEventType = MessageEventType.RECEIVED
    platform: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=200)
    customer_id: str = Field(min_length=1, max_length=500)
    customer_name: str | None = Field(default=None, max_length=200)
    message_id: str = Field(min_length=1, max_length=500)
    # A customer may send an image/video without a caption. Such an event is
    # valid when trusted inbound media metadata is present.
    message: str = Field(default="", max_length=10_000)
    timestamp: datetime
    direction: MessageDirection = MessageDirection.INBOUND
    status: MessageEventStatus = MessageEventStatus.DETECTED
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reply_task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_field_names(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        aliases = {
            "source_message_id": "message_id",
            "sender": "customer_name",
            "text": "message",
            "received_at": "timestamp",
        }
        for old, new in aliases.items():
            if new not in data and old in data:
                data[new] = data[old]
        data.setdefault("customer_id", data.get("customer_name") or data.get("conversation_id"))
        data.setdefault("message_id", data.get("id"))
        data.setdefault("timestamp", datetime.now(timezone.utc))
        return data

    @model_validator(mode="after")
    def normalize_and_validate(self):
        self.platform = self.platform.strip().casefold()
        self.conversation_id = self.conversation_id.strip()
        self.customer_id = self.customer_id.strip()
        self.customer_name = self.customer_name.strip() if self.customer_name else None
        self.message_id = self.message_id.strip()
        inbound_media = self.metadata.get("media") or []
        if (
            self.event_type == MessageEventType.RECEIVED
            and not self.message.strip()
            and not inbound_media
        ):
            raise ValueError("A received event needs text or inbound media")
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        if self.event_type == MessageEventType.RECEIVED and self.direction == MessageDirection.OUTBOUND:
            raise ValueError("A RECEIVED event cannot have OUTBOUND direction")
        if self.event_type == MessageEventType.SENT and self.direction == MessageDirection.INBOUND:
            raise ValueError("A SENT event cannot have INBOUND direction")
        return self

    @computed_field
    @property
    def dedup_key(self) -> str:
        normalized = "\0".join((
            self.platform,
            self.conversation_id.casefold().lstrip("@"),
            self.event_type.value,
            self.message_id,
        ))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def requires_reply(self) -> bool:
        return (
            self.event_type == MessageEventType.RECEIVED
            and self.direction == MessageDirection.INBOUND
            and self.status in {
                MessageEventStatus.NEEDS_REPLY,
                MessageEventStatus.APPROVAL_PENDING,
                MessageEventStatus.APPROVED,
            }
        )

    @property
    def source_message_id(self):
        return self.message_id

    @property
    def sender(self):
        return self.customer_name

    @property
    def text(self):
        return self.message

    @property
    def received_at(self):
        return self.timestamp
