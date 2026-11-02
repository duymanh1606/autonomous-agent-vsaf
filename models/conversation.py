from datetime import datetime, timezone

from pydantic import BaseModel, Field

from models.message_event import MessageDirection


class ConversationMessage(BaseModel):
    message_id: str
    conversation_id: str
    customer_id: str
    direction: MessageDirection
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
