import asyncio
import logging
from time import monotonic

from perception.state_parser import ParsedDomState, StateParser


logger = logging.getLogger(__name__)


class DomObserver:
    """Read-only Playwright DOM observation without screenshot or navigation.

    This is the entry point of the DOM-first pipeline:
    `DomObserver -> StateParser -> TargetResolver -> ActionExecutor -> StateVerifier`.
    It never clicks, never navigates by URL and never reloads the page.
    """

    def __init__(
        self,
        browser,
        state_parser=None,
        default_timeout_ms=4000,
        poll_interval_ms=150,
    ):
        self.browser = browser
        self.state_parser = state_parser or StateParser()
        self.default_timeout_ms = max(0, int(default_timeout_ms))
        # The poll interval only paces consecutive DOM reads. State is always
        # confirmed by re-reading the DOM, never by waiting a fixed time.
        self.poll_interval_ms = max(10, int(poll_interval_ms))

    async def snapshot(self):
        return await self.browser.snapshot()

    async def observe_state(self) -> ParsedDomState:
        return self.state_parser.parse(await self.snapshot())

    async def has_unread(self) -> bool:
        return (await self.observe_state()).has_unread

    async def wait_for(
        self, predicate, timeout_ms=None, poll_interval_ms=None, reason=""
    ):
        """Re-read the DOM until `predicate(state)` holds or the budget expires.

        Returns the last observed state. Callers decide what a timeout means;
        this method never raises on timeout so verification stays explicit.
        """
        budget = self.default_timeout_ms if timeout_ms is None else max(0, int(timeout_ms))
        interval = (
            self.poll_interval_ms if poll_interval_ms is None
            else max(10, int(poll_interval_ms))
        )
        deadline = monotonic() + budget / 1000
        attempts = 0
        state = await self.observe_state()
        while True:
            attempts += 1
            if predicate(state):
                logger.debug(
                    "[DOM] wait satisfied reason=%s attempts=%s screen=%s",
                    reason or "unspecified", attempts, state.screen,
                )
                return state
            if monotonic() >= deadline:
                logger.info(
                    "[DOM] wait timed out reason=%s attempts=%s screen=%s",
                    reason or "unspecified", attempts, state.screen,
                )
                return state
            await asyncio.sleep(interval / 1000)
            state = await self.observe_state()

    async def wait_for_screen(self, expected_screen, timeout_ms=None):
        return await self.wait_for(
            lambda state: state.is_screen(expected_screen),
            timeout_ms=timeout_ms,
            reason=f"screen={expected_screen}",
        )

    async def wait_for_message(self, text, timeout_ms=None):
        return await self.wait_for(
            lambda state: state.contains_message(text),
            timeout_ms=timeout_ms,
            reason="message_visible",
        )
