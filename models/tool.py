from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class InventoryResult(BaseModel):
    product_id: str
    size: str | None = None
    available: bool | None = None
    quantity: int | None = None
