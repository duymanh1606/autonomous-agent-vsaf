from grounding.result import GroundingResult


class GeminiGrounding:
    """Request grounding only from the configured Gemini vision observer."""

    def __init__(self, observer):
        self.observer = observer
        self.last_observation = None

    async def resolve(self, state, target, observation):
        grounded = await self.observer.observe(
            force_vision=True,
            grounding_target=target,
            allow_vision=True,
        )
        self.last_observation = grounded
        visual_target = max(
            (item for item in grounded.visual_targets if item.type == target),
            key=lambda item: item.confidence,
            default=None,
        )
        if visual_target is None:
            return None, grounded
        box = visual_target.bbox
        viewport = (grounded.metadata or {}).get("viewport") or {}
        return GroundingResult(
            x=round(box.x + box.width / 2),
            y=round(box.y + box.height / 2),
            source="gemini",
            confidence=visual_target.confidence,
            target=target,
            target_id=visual_target.target_id,
            viewport=(viewport.get("width"), viewport.get("height")),
            observation=grounded,
        ), grounded
