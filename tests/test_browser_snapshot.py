"""The snapshot contract: every key the rest of the agent reads must be there.

`snapshot()` is the one dictionary perception, authentication and message
delivery are all built on, and nothing exercised it. When six of its values
were deleted by accident the whole suite still passed, and the failure surfaced
only as `TikTok auth preflight stopped autonomous startup: TEMPORARY_ERROR` -
`check_auth` catches everything, so a `NameError` in this file was reported as
a flaky page.

Playwright is replaced by fakes here: the point is the shape of the result and
the fact that it is assembled at all, not how Chrome renders.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.auth import AuthState
from tools.browser import ChromeBrowserTool


class FakeLocator:
    """Enough of Playwright's locator for one snapshot.

    `visible` says whether the selector matches something on the page: that is
    how `snapshot()` decides between "logged in" and "a login form is up".
    """

    def __init__(self, text="", *, visible=False):
        self._text = text
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._visible else 0

    async def is_visible(self):
        return self._visible

    async def text_content(self, timeout=None):
        return None

    async def inner_text(self, timeout=None):
        return self._text

    async def evaluate_all(self, script):
        return []


class FakePage:
    """Answers the handful of calls `snapshot()` makes."""

    url = "https://www.tiktok.com/"
    is_closed = staticmethod(lambda: False)

    def __init__(self, *, authenticated=True, login_form=False):
        self.authenticated = authenticated
        self.login_form = login_form

    async def title(self):
        return "TikTok"

    def locator(self, selector):
        if selector == "body":
            return FakeLocator("trang chủ")
        login = "password" in selector or "username" in selector or "login" in selector
        if login:
            return FakeLocator(visible=self.login_form)
        return FakeLocator(visible=self.authenticated)

    async def evaluate(self, script, *args):
        # The layout probe is the only evaluate whose shape `snapshot()` reads.
        if "blocking_overlay_present" in script:
            return {
                "viewport": {"width": 1280, "height": 800},
                "blocking_overlay_present": False,
            }
        if "readyState" in script or "loading" in script:
            return {"ready": True, "loading": False}
        return []


class FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)

    async def cookies(self, urls=None):
        return self._cookies


def browser_with(page, context):
    """A tool wired to fakes, with `start()` bypassed."""
    tool = ChromeBrowserTool.__new__(ChromeBrowserTool)
    tool.__init__()
    tool._page = page
    tool._context = context
    tool._browser = None
    # `_ensure_started` would try to reach Chrome over CDP.
    async def _started():
        return None
    tool._ensure_started = _started
    return tool


class SnapshotCarriesEverythingItPromises(unittest.IsolatedAsyncioTestCase):
    REQUIRED = (
        "url", "title", "body_text", "elements", "signature",
        "session_cookie_names", "authenticated_dom", "login_form_visible",
        "page_render_ready", "page_loading", "viewport",
        "blocking_overlay_present", "ui_abnormal", "last_send_evidence",
        "extension_state",
    )

    async def _snapshot(self, cookies=()):
        tool = browser_with(FakePage(), FakeContext(cookies))
        return await tool.snapshot()

    async def test_every_key_the_agent_reads_is_present(self):
        snapshot = await self._snapshot()

        missing = [key for key in self.REQUIRED if key not in snapshot]
        self.assertEqual(missing, [], f"snapshot lost: {missing}")

    async def test_a_session_cookie_is_reported_as_authentication_evidence(self):
        snapshot = await self._snapshot(
            cookies=[{"name": "sessionid"}, {"name": "tt_csrf_token"}]
        )

        # Only session cookies count, and only their names ever leave here.
        self.assertEqual(snapshot["session_cookie_names"], ["sessionid"])

    async def test_check_auth_reports_the_state_instead_of_a_temporary_error(self):
        tool = browser_with(FakePage(), FakeContext([{"name": "sessionid"}]))

        result = await tool.check_auth()

        self.assertNotEqual(result["auth_state"], AuthState.TEMPORARY_ERROR.value)
        self.assertIn("session_cookie_names", result["signals"])

    async def test_a_broken_snapshot_is_reported_as_temporary_but_logged(self):
        """The catch-all stays - it just may not hide the cause any more."""
        tool = browser_with(FakePage(), FakeContext())

        async def broken():
            raise RuntimeError("chrome went away")

        tool.snapshot = broken
        with self.assertLogs("tools.browser", level="ERROR") as captured:
            result = await tool.check_auth()

        self.assertEqual(result["auth_state"], AuthState.TEMPORARY_ERROR.value)
        self.assertIn("chrome went away", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
