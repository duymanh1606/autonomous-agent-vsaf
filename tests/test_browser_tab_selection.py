"""Which tab the agent attaches to.

Chrome answers on its debugging port before it has finished creating the tab
it was launched with. An attach that lands in that window used to open a fresh
blank page and keep it: the extension only injects on tiktok.com, so it never
published a state, every observation fell back to the raw browser DOM, and the
recipient gate read the missing conversation header as "the wrong thread is
open". `tests/test_chrome_smoke.py` failed exactly this way on a loaded CI
runner while passing on a fast laptop.

Playwright is faked here - the question is which of the offered pages gets
picked, and how long the choice is allowed to wait for a better one.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.browser import ChromeBrowserTool


class FakePage:
    """A tab. `rendered` decides whether it looks loaded to the selector."""

    def __init__(self, url, *, rendered=True):
        self.url = url
        self.rendered = rendered

    async def evaluate(self, script):
        if not self.rendered:
            return {"text": 0, "nodes": 0}
        return {"text": 42, "nodes": 9}


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.opened = []

    async def new_page(self):
        page = FakePage("about:blank")
        self.opened.append(page)
        self.pages.append(page)
        return page


class FakeBrowser:
    """Contexts whose tabs may appear only after a few polls, the way a
    starting Chrome's do."""

    def __init__(self, pages, *, appears_on_poll=0, late_pages=()):
        self._context = FakeContext(list(pages))
        self._appears_on_poll = appears_on_poll
        self._late_pages = list(late_pages)
        self.polls = 0

    @property
    def contexts(self):
        self.polls += 1
        if self._late_pages and self.polls > self._appears_on_poll:
            self._context.pages.extend(self._late_pages)
            self._late_pages = []
        return [self._context]

    @property
    def context(self):
        return self._context


class TabSelectionTests(unittest.IsolatedAsyncioTestCase):
    def _tool(self, browser, *, budget=1.0):
        tool = ChromeBrowserTool(
            cdp_url="http://127.0.0.1:0",
            screenshot_dir="/tmp",
            base_url="https://www.tiktok.com",
        )
        tool._browser = browser
        tool.TAB_WAIT_SECONDS = budget
        return tool

    async def test_a_rendered_tiktok_tab_is_taken_at_once(self):
        loaded = FakePage("https://www.tiktok.com/messages")
        browser = FakeBrowser([FakePage("about:blank"), loaded])
        tool = self._tool(browser)

        _, page = await tool._select_context_and_page()

        self.assertIs(page, loaded)
        self.assertEqual(browser.polls, 1, "a tab already there must not be waited for")
        self.assertEqual(browser.context.opened, [])

    async def test_a_tab_that_arrives_late_is_waited_for(self):
        """The CI failure: Chrome's port was up, its tab was not."""
        late = FakePage("https://www.tiktok.com/messages")
        browser = FakeBrowser([], appears_on_poll=2, late_pages=[late])
        tool = self._tool(browser)

        _, page = await tool._select_context_and_page()

        self.assertIs(page, late)
        # No blank page was opened in the meantime, so the extension has a
        # page it can actually inject on.
        self.assertEqual(browser.context.opened, [])

    async def test_a_tiktok_tab_still_loading_gets_the_rest_of_the_budget(self):
        loading = FakePage("https://www.tiktok.com/messages", rendered=False)
        browser = FakeBrowser([loading])
        tool = self._tool(browser, budget=0.5)

        _, page = await tool._select_context_and_page()

        # It never rendered, but it is still the right tab - far better than a
        # blank one.
        self.assertIs(page, loading)
        self.assertEqual(browser.context.opened, [])
        self.assertGreater(browser.polls, 1)

    async def test_no_tiktok_tab_at_all_still_yields_a_page(self):
        """With nothing to attach to the agent is useless either way; it must
        not hang, and the blank page it gets is logged as the problem it is."""
        browser = FakeBrowser([FakePage("https://www.google.com/")])
        tool = self._tool(browser, budget=0.3)

        with self.assertLogs("tools.browser", level="WARNING") as logs:
            _, page = await tool._select_context_and_page()

        self.assertEqual(page.url, "about:blank")
        self.assertEqual(len(browser.context.opened), 1)
        self.assertIn("No TikTok tab", "".join(logs.output))

    async def test_a_browser_with_no_context_is_an_error(self):
        class Empty:
            contexts = []

        with self.assertRaises(RuntimeError):
            await self._tool(Empty())._select_context_and_page()


if __name__ == "__main__":
    unittest.main()
