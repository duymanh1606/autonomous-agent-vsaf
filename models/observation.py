from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from models.auth import AuthState
from models.message_delivery import ComposerState, MessageSendState


class BoundingBox(BaseModel):
    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)
    width: float = Field(ge=0.0)
    height: float = Field(ge=0.0)


class VisualTarget(BaseModel):
    target_id: str
    type: Literal[
        "inbox_button", "conversation", "conversation_item",
        "unread_conversation", "message_input", "send_button", "back_button",
        "popup_close", "other"
    ] = "other"
    text: str = ""
    description: str = ""
    bbox: BoundingBox
    confidence: float = Field(ge=0.0, le=1.0)


class Observation(BaseModel):

    screen: str | None

    confidence: float = 0.0

    screen_confidence: float = 0.0

    perception_source: Literal[
        "dom", "extension", "vision", "local", "gemini",
        "dom+vision", "extension+vision"
    ] = "dom"

    visual_targets: list[VisualTarget] = Field(default_factory=list)

    message_send_state: MessageSendState = MessageSendState.UNCERTAIN

    composer_state: ComposerState = ComposerState.UNKNOWN

    composer_text_matches_expected: bool | None = None

    last_message_visible: bool | None = None

    elements: list[dict] = Field(default_factory=list)

    screenshot_path: str | None = None

    raw_ui_xml: str | None = None

    unknown: bool = False

    security_challenge: bool = False

    auth_state: AuthState = AuthState.UNKNOWN

    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keep_confidence_fields_compatible(self):
        if self.screen_confidence == 0.0 and self.confidence != 0.0:
            self.screen_confidence = self.confidence
        elif self.confidence == 0.0 and self.screen_confidence != 0.0:
            self.confidence = self.screen_confidence
        return self
