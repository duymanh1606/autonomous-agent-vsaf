import logging
from time import monotonic


logger = logging.getLogger(__name__)


class ActionExecutor:
    """Click a resolved DOM target, preferring its live locator.

    The executor only performs the click. It never waits a fixed time and
    never decides whether the click worked; `StateVerifier` re-reads the DOM
    for that.
    """

    def __init__(self, browser):
        self.browser = browser

    async def click(self, target, expected_screen=None):
        await self.browser._ensure_started()
        previous_sequence = await self.browser.current_extension_sequence()
        point = dict(target.click_point)
        bbox = dict(target.bbox)
        method = "coordinate"
        locator = target.locator

        if locator is not None and await self._locator_is_live(locator):
            live_box = await locator.bounding_box()
            if live_box is not None and live_box["width"] > 0 and live_box["height"] > 0:
                # The locator is still attached: let Playwright handle
                # scrolling, actionability and hit-target checks.
                await locator.click()
                bbox = {
                    "x": live_box["x"], "y": live_box["y"],
                    "width": live_box["width"], "height": live_box["height"],
                }
                point = {
                    "x": live_box["x"] + live_box["width"] / 2,
                    "y": live_box["y"] + live_box["height"] / 2,
                }
                method = "locator"
            else:
                # Attached but without a usable box: fall back to the point
                # measured when the target was resolved.
                await self.browser._page.mouse.click(point["x"], point["y"])
        else:
            await self.browser._page.mouse.click(point["x"], point["y"])

        clicked_point = (point["x"], point["y"])
        logger.info(
            "[UI] click target=%s method=%s point=(%.0f,%.0f) expected_screen=%s",
            target.name, method, point["x"], point["y"], expected_screen or "none",
        )
        return {
            "success": True,
            "source": "dom",
            "target": target.name,
            "normalized_target": {**target.as_dict(), "bbox": bbox, "click_point": point},
            "click_method": method,
            "predicted_point": clicked_point,
            "clicked_point": clicked_point,
            "previous_extension_sequence": previous_sequence,
            "expected_screen": expected_screen,
            "action_started_at": monotonic(),
        }

    @staticmethod
    async def _locator_is_live(locator):
        try:
            return bool(await locator.count()) and await locator.is_visible()
        except Exception:  # noqa: BLE001 - a detached locator raises
            # A detached locator raises instead of returning False; treat that
            # as "not live" and use the measured coordinate.
            return False
