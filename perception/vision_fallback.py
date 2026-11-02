import base64
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models.observation import BoundingBox, VisualTarget
from models.message_delivery import ComposerState, MessageSendState
from platforms.tiktok.screens import TikTokScreen


logger = logging.getLogger(__name__)


VISION_SCREEN_MAP = {
    "feed": TikTokScreen.FEED,
    "messages": TikTokScreen.MESSAGES,
    "conversation": TikTokScreen.CONVERSATION,
    "profile": TikTokScreen.PROFILE,
    "login": TikTokScreen.LOGIN,
    "security_challenge": TikTokScreen.SECURITY_CHALLENGE,
    "unknown": TikTokScreen.UNKNOWN,
}


class _RawVisualTarget(BaseModel):
    type: str = "other"
    text: str = ""
    description: str = ""
    bbox: BoundingBox
    confidence: float = Field(ge=0.0, le=1.0)


class VisibleConversationMessage(BaseModel):
    direction: str
    text: str = Field(min_length=1, max_length=10_000)

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, value):
        normalized = str(value).strip().upper()
        if normalized not in {"INBOUND", "OUTBOUND", "UNKNOWN"}:
            raise ValueError("Unsupported message direction")
        return normalized


class ExtractedCustomerFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    product_interest: str | None = None
    size: str | None = None
    color: str | None = None
    quantity: str | None = None
    purchase_note: str | None = None


class _RawVisionResult(BaseModel):
    screen: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    targets: list[_RawVisualTarget] = Field(default_factory=list)
    message_send_state: MessageSendState = MessageSendState.UNCERTAIN
    composer_state: ComposerState = ComposerState.UNKNOWN
    composer_text_matches_expected: bool | None = None
    last_message_visible: bool | None = None
    messages: list[VisibleConversationMessage] = Field(default_factory=list)
    customer_facts: ExtractedCustomerFacts = Field(
        default_factory=ExtractedCustomerFacts
    )
    status: Literal["ok", "uncertain"] = "ok"


_RESPONSE_JSON_SCHEMA = _RawVisionResult.model_json_schema()


class VisionResult(BaseModel):
    screen: str = TikTokScreen.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    targets: list[VisualTarget] = Field(default_factory=list)
    message_send_state: MessageSendState = MessageSendState.UNCERTAIN
    composer_state: ComposerState = ComposerState.UNKNOWN
    composer_text_matches_expected: bool | None = None
    last_message_visible: bool | None = None
    messages: list[VisibleConversationMessage] = Field(default_factory=list)
    customer_facts: dict[str, str] = Field(default_factory=dict)
    status: Literal["ok", "uncertain"] = "ok"
    source: str = "vision"


class VisionFallback:
    """Analyze screenshots only; this component never performs UI actions."""

    _PROMPT = """Analyze this TikTok Web screenshot for perception and verification only. Return exactly one JSON object and no prose.
Use this schema:
{"screen":"messages|conversation|profile|feed|login|security_challenge|unknown","confidence":0.0,"status":"ok|uncertain","message_send_state":"SENT|NOT_SENT|UNCERTAIN","composer_state":"EMPTY|HAS_TEXT|UNKNOWN","composer_text_matches_expected":null,"last_message_visible":null,"targets":[{"type":"inbox_button|conversation|conversation_item|unread_conversation|message_input|send_button|back_button|popup_close|other","text":"","description":"","bbox":{"x":0,"y":0,"width":0,"height":0},"confidence":0.0}],"messages":[{"direction":"INBOUND|OUTBOUND|UNKNOWN","text":""}],"customer_facts":{"name":null,"phone":null,"email":null,"address":null,"city":null,"product_interest":null,"size":null,"color":null,"quantity":null,"purchase_note":null}}
Coordinates are CSS viewport pixels from the screenshot's top-left. Include only clearly visible targets and messages. Never infer hidden or cropped text. Extract customer facts only from text visibly sent by the customer; never infer age, gender, income, identity, current location, or sensitive data that was not explicitly provided. When conversation extraction is disabled, return messages=[] and an empty customer_facts object. SENT requires the expected message to be visibly present as a sent chat message. NOT_SENT requires the composer to visibly retain the exact expected text and the expected sent message not to be visible. An empty composer without a visible message is UNCERTAIN. If uncertain, use low confidence and UNCERTAIN. Never propose or execute actions, retry sending, or attempt to solve CAPTCHA, login, OTP, or security verification."""

    _GROUNDING_REQUESTS = {
        "inbox_button": "Locate the Inbox or Messages navigation button",
        "conversation_item": "Locate the requested conversation item",
        "unread_conversation": "Locate the requested unread conversation",
        "message_input": "Locate the message input",
        "send_button": "Locate the send button",
        "back_button": "Locate the visible back button",
        "popup_close": "Locate the close button for the visible popup",
    }

    def __init__(
        self,
        analyzer=None,
        unknown_screen=TikTokScreen.UNKNOWN,
        enabled=True,
        max_targets=20,
        reasoning_progress_seconds=10,
        layer_name="vision",
    ):
        self.analyzer = analyzer
        self.unknown_screen = unknown_screen
        self.enabled = bool(enabled)
        self.max_targets = max(0, int(max_targets))
        self.reasoning_progress_seconds = max(0.0, float(reasoning_progress_seconds))
        self.layer_name = str(layer_name)

    async def analyze(
        self, screenshot_path, expected_message=None, verification_action=None,
        grounding_target=None, extract_conversation=False,
    ):
        if not self.enabled or self.analyzer is None:
            return VisionResult(screen=self.unknown_screen, source=self.layer_name)
        try:
            if self.layer_name == "gemini":
                logger.info("[VISION] Gemini analysis started")
            image = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")
            context = json.dumps({
                "verification_action": verification_action,
                "expected_message": expected_message,
                "grounding_target": grounding_target,
                "extract_conversation": bool(extract_conversation),
            }, ensure_ascii=False)
            raw = None
            grounding_request = self._GROUNDING_REQUESTS.get(grounding_target)
            prompt = f"{self._PROMPT}\nVERIFICATION CONTEXT: {context}"
            if grounding_request:
                prompt += (
                    f"\nGROUNDING REQUEST: {grounding_request}. Return a target with "
                    f'type="{grounding_target}" only when clearly visible.'
                )
            if hasattr(self.analyzer, "generate"):
                raw = await self._analyze_with_progress(
                    self.analyzer.generate(
                        prompt, temperature=0.0, json_mode=True, images=[image],
                        response_schema=_RESPONSE_JSON_SCHEMA,
                    )
                )
            else:
                raw = await self._analyze_with_progress(
                    self.analyzer(screenshot_path, prompt)
                )
            payload = self._parse_json_object(raw)
            raw_targets = payload.get("targets", [])
            payload["targets"] = []
            payload["message_send_state"] = self._normalize_enum(
                payload.get("message_send_state"), "UNCERTAIN",
            )
            payload["composer_state"] = self._normalize_enum(
                payload.get("composer_state"), "UNKNOWN",
            )
            parsed = _RawVisionResult.model_validate(payload)
            parsed.targets = []
            for target_payload in raw_targets[:self.max_targets]:
                try:
                    parsed.targets.append(_RawVisualTarget.model_validate(target_payload))
                except ValidationError:
                    # One malformed target must not discard a valid screen
                    # classification and verification result.
                    continue
        except Exception as exc:
            # error_type alone was never enough to tell a truncated/malformed
            # JSON response apart from a transport failure or a schema
            # mismatch - log the real message and a bounded snippet of what
            # the model actually returned so a recurrence is diagnosable
            # instead of just "JSONDecodeError" with no further trace.
            snippet = raw[:400] if isinstance(raw, str) else None
            logger.warning(
                "Vision response rejected: error_type=%s error=%s raw_snippet=%r",
                type(exc).__name__, exc, snippet, exc_info=True,
            )
            return VisionResult(screen=self.unknown_screen, source=self.layer_name)

        screen_name = parsed.screen.strip().casefold()
        if screen_name.startswith("tiktok_"):
            screen_name = screen_name.removeprefix("tiktok_")
        screen = VISION_SCREEN_MAP.get(screen_name, self.unknown_screen)
        confidence = parsed.confidence if screen_name in VISION_SCREEN_MAP else 0.0
        allowed_types = {
            "inbox_button", "conversation", "conversation_item",
            "unread_conversation", "message_input", "send_button", "back_button",
            "popup_close", "other",
        }
        targets = []
        screenshot_key = hashlib.sha256(str(screenshot_path).encode()).hexdigest()[:16]
        for index, target in enumerate(parsed.targets[:self.max_targets]):
            if target.bbox.width <= 0 or target.bbox.height <= 0:
                continue
            target_type = target.type.strip().casefold()
            if target_type not in allowed_types:
                target_type = "other"
            targets.append(VisualTarget(
                target_id=f"visual-{screenshot_key}-{index}",
                type=target_type,
                text=target.text[:200],
                description=target.description[:500],
                bbox=target.bbox,
                confidence=target.confidence,
            ))
        if screen in {TikTokScreen.LOGIN, TikTokScreen.SECURITY_CHALLENGE}:
            targets = []
            parsed.messages = []
            parsed.customer_facts = ExtractedCustomerFacts()
        facts = {
            key: str(value).strip()
            for key, value in parsed.customer_facts.model_dump(exclude_none=True).items()
            if str(value).strip()
        }
        if not extract_conversation or screen != TikTokScreen.CONVERSATION:
            parsed.messages = []
            facts = {}
        if self.layer_name == "gemini":
            logger.info(
                "[VISION] screen=%s confidence=%.2f",
                str(screen).removeprefix("TIKTOK_"), confidence,
            )
            logger.info("[VISION] messages_extracted=%s", len(parsed.messages))
            logger.info("[VISION] customer_facts_extracted=%s", len(facts))
        return VisionResult(
            screen=screen,
            confidence=confidence,
            targets=targets,
            message_send_state=parsed.message_send_state,
            composer_state=parsed.composer_state,
            composer_text_matches_expected=parsed.composer_text_matches_expected,
            last_message_visible=parsed.last_message_visible,
            messages=parsed.messages,
            customer_facts=facts,
            status=parsed.status,
            source=self.layer_name,
        )

    @staticmethod
    def _parse_json_object(raw):
        if isinstance(raw, dict):
            return dict(raw)
        if not isinstance(raw, str):
            raise TypeError("Vision response must be a JSON object")
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Vision response contains no JSON object")
        payload = json.loads(text[start:end + 1])
        if not isinstance(payload, dict):
            raise TypeError("Vision response JSON must be an object")
        return payload

    @staticmethod
    def _normalize_enum(value, default):
        if value is None:
            return default
        normalized = str(value).strip().upper()
        return normalized or default

    async def _analyze_with_progress(self, awaitable):
        request = asyncio.create_task(awaitable)
        elapsed = 0.0
        try:
            if self.reasoning_progress_seconds <= 0:
                return await request
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(request),
                        timeout=self.reasoning_progress_seconds,
                    )
                except asyncio.TimeoutError:
                    elapsed += self.reasoning_progress_seconds
                    logger.info(
                        "Vision reasoning in progress: elapsed_seconds=%s",
                        int(elapsed),
                    )
        finally:
            if not request.done():
                request.cancel()
