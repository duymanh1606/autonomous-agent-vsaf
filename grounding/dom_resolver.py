import logging

from grounding.result import GroundingResult
from models.dom_target import DomTarget
from platforms.tiktok_web.selectors import (
    TARGET_SELECTORS,
    UNREAD_ATTRIBUTE_VALUES,
    UNREAD_DESCENDANT_SELECTOR,
    UNREAD_LABEL_WORDS,
)


logger = logging.getLogger(__name__)

# Targets whose identity depends on the visible row text, so `target_text`
# filters the candidate set through a parent-child relation instead of index.
TEXT_SCOPED_TARGETS = frozenset({"conversation", "unread_conversation"})

MAX_CANDIDATES_PER_SELECTOR = 100

# A box smaller than this in either dimension is treated the same as "no
# bounding box": a decorative child (an unread dot, an avatar mask, a
# collapsed wrapper) can be "visible" and "enabled" under Playwright's rules
# while being nowhere near large enough to be the real clickable row. Without
# this floor such a node wins the very first candidate and the click lands on
# a degenerate point (e.g. click_point=(1,100)) that never opens anything.
MIN_CLICKABLE_DIMENSION_PX = 10


class DomResolver:
    """Resolve stable TikTok targets from live Playwright locators first.

    Selector strategies are ordered `data-* -> role/aria-label -> structure ->
    text`; hashed CSS class names are never used. A target is only returned
    when it is visible, enabled and has a real bounding box, so callers can
    treat `None` as "escalate to screenshot/vision".
    """

    _TARGET_IDS = {
        # `inbox-icon` is the Activity entry point and never Messages.
        "messages_button": {"nav-message", "nav-messages", "top-dm-icon"},
        "message_input": {"dm-new-input-editor", "message-input-area"},
        "send_button": {"message-send"},
        "conversation": {"dm-new-conversation-item"},
        "unread_conversation": {"dm-new-conversation-item"},
    }

    NOT_FOUND = "not_found"
    NO_BOUNDING_BOX = "no_bounding_box"

    def __init__(self, browser=None, selectors=None, target_selectors=None):
        self.browser = browser
        self.selectors = selectors or {}
        self.target_selectors = target_selectors or TARGET_SELECTORS
        self.last_failure = None

    async def resolve(
        self, state, target, observation, *, target_text=None, unread_only=False,
        selector=None,
    ):
        dom_target = await self.resolve_target(
            target, target_text=target_text, unread_only=unread_only,
            selector=selector,
        )
        if dom_target is not None:
            return GroundingResult(
                x=round(dom_target.click_point["x"]),
                y=round(dom_target.click_point["y"]),
                source="dom",
                confidence=1.0,
                target=dom_target.name,
                viewport=self._viewport(observation),
                dom_target=dom_target,
            )
        return self._resolve_observed(target, observation)

    async def resolve_target(
        self, target, *, target_text=None, unread_only=False, selector=None
    ) -> DomTarget | None:
        """Return the normalized target or None when DOM evidence is missing.

        `selector` is the stable CSS selector the extension recorded for this
        exact element. It is tried first, so a click uses the DOM handle the
        perception layer actually saw instead of a coordinate.
        """
        if self.browser is None:
            self.last_failure = self.NOT_FOUND
            return None
        await self.browser._ensure_started()
        key = self._normalize_target(target)
        require_unread = key == "unread_conversation" or unread_only
        missing_box = False
        for strategy, matched_selector, locator in self._candidate_locators(
            key, target_text, selector_hint=selector
        ):
            for index in range(min(await locator.count(), MAX_CANDIDATES_PER_SELECTOR)):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                if require_unread and not await self._is_unread(candidate):
                    continue
                if not await candidate.is_enabled():
                    continue
                # Requirement: the click point always comes from the live box.
                box = await candidate.bounding_box()
                if (
                    box is None
                    or box["width"] < MIN_CLICKABLE_DIMENSION_PX
                    or box["height"] < MIN_CLICKABLE_DIMENSION_PX
                ):
                    # An element can be attached and "visible" yet have no box
                    # (collapsed ancestor, off-screen transform) or a box too
                    # small to be the real clickable row (a badge, a divider).
                    # Either is the documented trigger for the screenshot/
                    # vision fallback - never a click on a degenerate point.
                    missing_box = True
                    continue
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                self.last_failure = None
                logger.info(
                    "[DOM] target resolved name=%s strategy=%s click_point=(%.0f,%.0f)",
                    key, strategy, x, y,
                )
                return DomTarget(
                    name=key,
                    visible=True,
                    clickable=True,
                    bbox={
                        "x": box["x"], "y": box["y"],
                        "width": box["width"], "height": box["height"],
                    },
                    click_point={"x": x, "y": y},
                    locator=candidate,
                    selector=matched_selector,
                    text=target_text,
                )
        self.last_failure = self.NO_BOUNDING_BOX if missing_box else self.NOT_FOUND
        logger.info(
            "[DOM] target unresolved name=%s reason=%s; escalating to fallback",
            key, self.last_failure,
        )
        return None

    async def resolve_unread_conversations(self, limit=5):
        """List currently unread conversation rows with their click points."""
        if self.browser is None:
            return []
        await self.browser._ensure_started()
        found = []
        seen = set()
        for _, selector, locator in self._candidate_locators("unread_conversation", None):
            for index in range(min(await locator.count(), MAX_CANDIDATES_PER_SELECTOR)):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                if not await self._is_unread(candidate):
                    continue
                name = await self._row_name(candidate)
                if not name or name in seen:
                    continue
                box = await candidate.bounding_box()
                if (
                    box is None
                    or box["width"] < MIN_CLICKABLE_DIMENSION_PX
                    or box["height"] < MIN_CLICKABLE_DIMENSION_PX
                ):
                    continue
                seen.add(name)
                found.append(DomTarget(
                    name="unread_conversation",
                    visible=True,
                    clickable=True,
                    bbox={
                        "x": box["x"], "y": box["y"],
                        "width": box["width"], "height": box["height"],
                    },
                    click_point={
                        "x": box["x"] + box["width"] / 2,
                        "y": box["y"] + box["height"] / 2,
                    },
                    locator=candidate,
                    selector=selector,
                    text=name,
                ))
                if len(found) >= max(1, int(limit)):
                    return found
        return found

    async def _row_name(self, row):
        for _, selector, _ in self._strategies("conversation_nickname"):
            nickname = row.locator(selector).first
            if await nickname.count() and await nickname.is_visible():
                value = (await nickname.inner_text()).strip()
                if value:
                    return value
        text = (await row.inner_text()).strip()
        return text.splitlines()[0].strip() if text else ""

    def _strategies(self, key):
        """Yield (strategy, selector, None) pairs from the target catalog."""
        entries = self.target_selectors.get(key)
        if entries:
            return [(strategy, selector, None) for strategy, selector in entries]
        legacy_key = {
            "messages_button": "inbox",
            "message_input": "message_input",
            "send_button": "send_message",
            "conversation": "conversation_item",
            "unread_conversation": "conversation_item",
            "conversation_nickname": "conversation_nickname",
        }.get(key, key)
        return [
            ("legacy", selector, None)
            for selector in self.selectors.get(legacy_key, ())
        ]

    def _candidate_locators(self, key, target_text, selector_hint=None):
        page = self.browser._page
        candidates = []
        if selector_hint:
            # Strongest evidence: the selector the extension recorded for the
            # element it reported in this very state.
            try:
                candidates.append(
                    ("extension_selector", selector_hint, page.locator(selector_hint))
                )
            except Exception:  # noqa: BLE001 - an extension selector the page rejects is expected
                logger.info(
                    "[DOM] extension selector unusable name=%s selector=%s",
                    key, selector_hint,
                )
        for strategy, selector, _ in self._strategies(key):
            locator = page.locator(selector)
            if target_text and key in TEXT_SCOPED_TARGETS:
                # Parent-child relation: keep only rows that contain the exact
                # visible name, instead of trusting the row order.
                locator = locator.filter(has=page.get_by_text(target_text, exact=True))
            candidates.append((strategy, selector, locator))
        return candidates

    def _resolve_observed(self, target, observation):
        wanted = self._normalize_target(target)
        resource_ids = self._TARGET_IDS.get(wanted, {wanted})
        for element in observation.elements if observation else ():
            if not element.get("visible", True):
                continue
            if str(element.get("resource_id") or "").casefold() not in resource_ids:
                continue
            box = element.get("bbox") or {}
            try:
                width, height = float(box["width"]), float(box["height"])
                x = round(float(box["x"]) + width / 2)
                y = round(float(box["y"]) + height / 2)
            except (KeyError, TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            return GroundingResult(
                x=x, y=y, source="dom", confidence=1.0,
                target=wanted, viewport=self._viewport(observation),
            )
        return None

    @staticmethod
    async def _is_unread(locator):
        unread_value = str(await locator.get_attribute("data-unread") or "").casefold()
        if unread_value in UNREAD_ATTRIBUTE_VALUES:
            return True
        label = str(await locator.get_attribute("aria-label") or "").casefold()
        if any(word in label for word in UNREAD_LABEL_WORDS):
            return True
        signals = locator.locator(UNREAD_DESCENDANT_SELECTOR)
        for index in range(min(await signals.count(), 10)):
            if await signals.nth(index).is_visible():
                return True
        return False

    @staticmethod
    def _normalize_target(target):
        value = str(target or "").strip().casefold()
        return {
            "messages": "messages_button",
            "inbox_button": "messages_button",
            "conversation_item": "conversation",
        }.get(value, value)

    @staticmethod
    def _viewport(observation):
        viewport = ((observation.metadata or {}).get("viewport") if observation else {}) or {}
        return (viewport.get("width"), viewport.get("height"))
