from typing import Any

from pydantic import BaseModel, Field

class Action(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
