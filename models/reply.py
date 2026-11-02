from pydantic import BaseModel, Field


class ReplyResult(BaseModel):
    text: str = Field(default="", max_length=1000)
    should_send: bool = False
    requires_human: bool = False
    model: str | None = None


class ReplyValidation(BaseModel):
    allowed: bool
    reason: str | None = None
