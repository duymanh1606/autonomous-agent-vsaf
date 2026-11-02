from typing import Any

from pydantic import BaseModel, Field


class ActionResult(BaseModel):

    success: bool

    data: dict[str, Any] = Field(default_factory=dict)

    error: str | None = None


    
