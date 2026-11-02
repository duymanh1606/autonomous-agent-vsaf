from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


DEFAULT_CUSTOMER_STAGES = frozenset({
    "new", "browsing", "asking_product", "asking_price",
    "checking_inventory", "interested", "ordering", "after_sales",
    "complaint", "human_required", "closed",
})


class CustomerState(BaseModel):
    customer_id: str
    conversation_id: str
    intent: str | None = None
    stage: str = "new"
    product_id: str | None = None
    product_name: str | None = None
    size: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    sentiment: str = "neutral"
    conversation_summary: str = ""
    requires_tool: bool = False
    required_tool: str | None = None
    requires_human: bool = False
    last_message: str = ""
    message_count: int = Field(default=0, ge=0)
    unknown_intent_count: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
