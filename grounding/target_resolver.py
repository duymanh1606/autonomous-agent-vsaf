class TargetResolver:
    """Resolve targets in DOM -> fixed coordinate -> Gemini order."""

    def __init__(self, dom_resolver, fixed_resolver, gemini_grounding):
        self.dom = dom_resolver
        self.fixed = fixed_resolver
        self.gemini = gemini_grounding

    async def resolve(
        self,
        state,
        target,
        observation,
        *,
        allowed_sources=("dom", "fixed_coordinate", "gemini"),
        expected_state=None,
        gemini_target=None,
        target_text=None,
        unread_only=False,
    ):
        if "dom" in allowed_sources:
            result = await self.dom.resolve(
                state, target, observation,
                target_text=target_text,
                unread_only=unread_only,
            )
            if result is not None:
                return result
        if "fixed_coordinate" in allowed_sources:
            result = self.fixed.resolve_grounding(
                state, target, observation, expected_state=expected_state
            )
            if result is not None:
                return result
        if "gemini" in allowed_sources:
            result, grounded = await self.gemini.resolve(
                state, gemini_target or target, observation
            )
            return result
        return None
