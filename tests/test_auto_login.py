import unittest
from time import monotonic
from unittest.mock import AsyncMock

from models.auth import AuthState
from tools.browser import ChromeBrowserTool


class FakeLocator:
    def __init__(self, visible=True, on_click=None, box=None):
        self.visible = visible
        self.on_click = on_click
        self.box = box or {"x": 100, "y": 100, "width": 200, "height": 40}
        self.filled = []
        self.clicks = 0

    @property
    def first(self):
        return self

    def nth(self, index):
        assert index == 0, "FakeLocator.count() always reports 1 match"
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        return self.visible

    async def bounding_box(self):
        return dict(self.box) if self.visible else None

    async def fill(self, value):
        self.filled.append(value)

    async def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()


class FakePage:
    def __init__(self):
        self.google = FakeLocator(box={"x": 300, "y": 400, "width": 240, "height": 50})
        self.login = FakeLocator(
            visible=False, on_click=self._open_login,
            box={"x": 900, "y": 40, "width": 120, "height": 44},
        )
        self.mouse = FakeMouse(self)
        self.waits = []

    def _open_login(self):
        self.google.visible = True

    def locator(self, selector):
        if selector == 'button:has-text("Continue with Google")':
            return self.google
        if selector == '[data-e2e="top-login-button"]':
            return self.login
        return FakeLocator(visible=False)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def evaluate(self, _script):
        return {"width": 1280, "height": 900}


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))
        for locator in (self.page.login, self.page.google):
            box = locator.box
            if (
                locator.visible
                and box["x"] <= x <= box["x"] + box["width"]
                and box["y"] <= y <= box["y"] + box["height"]
            ):
                await locator.click()
                return


def auth(state):
    return {"success": state == AuthState.LOGGED_IN, "auth_state": state.value}


class AutoLoginTests(unittest.IsolatedAsyncioTestCase):
    def browser(self):
        browser = ChromeBrowserTool(
            auto_login_enabled=True,
            auto_login_max_attempts=2,
            auto_login_cooldown_seconds=300,
            auto_login_post_submit_wait_seconds=0,
        )
        browser._page = FakePage()
        return browser

    async def test_google_continue_is_clicked_and_login_is_verified(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(side_effect=[
            auth(AuthState.AUTH_REQUIRED), auth(AuthState.LOGGED_IN),
        ])

        result = await browser.auto_login()

        self.assertEqual(result["auth_state"], AuthState.LOGGED_IN.value)
        self.assertEqual(result["reason"], "login_confirmed")
        self.assertEqual(browser._page.google.clicks, 1)

    async def test_cooldown_prevents_a_second_submit(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(return_value=auth(AuthState.AUTH_REQUIRED))

        first = await browser.auto_login()
        second = await browser.auto_login()

        self.assertTrue(first["attempted"])
        self.assertFalse(second["attempted"])
        self.assertEqual(second["reason"], "cooldown")
        self.assertEqual(browser._page.google.clicks, 1)

    async def test_feed_login_button_opens_modal_then_google_is_clicked(self):
        browser = self.browser()
        browser._page.google.visible = False
        browser._page.login.visible = True
        browser.check_auth = AsyncMock(return_value=auth(AuthState.AUTH_REQUIRED))

        result = await browser.auto_login()

        self.assertTrue(result["attempted"])
        self.assertEqual(browser._page.login.clicks, 1)
        self.assertEqual(browser._page.google.clicks, 1)
        self.assertEqual(browser._page.mouse.clicks, [(960.0, 62.0), (420.0, 425.0)])
        self.assertIn(1000, browser._page.waits)

    async def test_security_challenge_is_never_submitted(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(return_value=auth(AuthState.SECURITY_CHALLENGE))

        result = await browser.auto_login()

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "human_verification_required")
        self.assertEqual(browser._page.google.clicks, 0)

    async def test_fedcm_selects_first_account_after_settle(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(side_effect=[
            auth(AuthState.AUTH_REQUIRED), auth(AuthState.LOGGED_IN),
        ])

        class Session:
            def __init__(self):
                self.commands = []

            async def send(self, command, parameters):
                self.commands.append((command, parameters))

        browser._fedcm_session = Session()
        browser._fedcm_dialog = {
            "dialogId": "dialog-1",
            "dialogType": "AccountChooser",
            "accounts": [{"email": "first@example.com"}, {"email": "second@example.com"}],
        }
        browser._fedcm_dialog_shown_at = monotonic() - 5

        result = await browser.auto_login()

        self.assertEqual(result["auth_state"], AuthState.LOGGED_IN.value)
        self.assertEqual(browser._fedcm_session.commands, [(
            "FedCm.selectAccount", {"dialogId": "dialog-1", "accountIndex": 0}
        )])

    async def test_fedcm_continues_current_flow_during_outer_cooldown(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(return_value=auth(AuthState.AUTH_REQUIRED))

        class Session:
            def __init__(self):
                self.commands = []

            async def send(self, command, parameters):
                self.commands.append((command, parameters))

        browser._fedcm_session = Session()
        browser._fedcm_dialog = {
            "dialogId": "dialog-2",
            "dialogType": "AccountChooser",
            "accounts": [{"email": "first@example.com"}],
        }
        browser._fedcm_dialog_shown_at = monotonic() - 5
        browser._google_flow_active = True
        browser._auto_login_attempts = 1
        browser._auto_login_next_at = monotonic() + 300

        result = await browser.auto_login()

        self.assertTrue(result["attempted"])
        self.assertEqual(browser._auto_login_attempts, 1)
        self.assertEqual(browser._fedcm_session.commands[0][0], "FedCm.selectAccount")

    async def test_existing_google_window_prevents_another_click(self):
        browser = self.browser()
        browser.check_auth = AsyncMock(return_value=auth(AuthState.AUTH_REQUIRED))

        class GooglePage:
            url = "https://accounts.google.com/o/oauth2/auth"

            @staticmethod
            def is_closed():
                return False

        browser._context = type("Context", (), {"pages": [browser._page, GooglePage()]})()

        result = await browser.auto_login()

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "google_window_open")
        self.assertEqual(browser._page.google.clicks, 0)


if __name__ == "__main__":
    unittest.main()
