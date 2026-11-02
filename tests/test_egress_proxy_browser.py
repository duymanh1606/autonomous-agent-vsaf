"""The CDP side of egress-ip-gateway: answering the proxy's 407 without ever
letting Chrome pause a request with nobody to release it, and proving the
tab's traffic really leaves through the workspace IP before any customer
conversation is touched.

Playwright is replaced by fakes - what is under test is the protocol
discipline (handlers before enable, every paused request continued, only
proxy challenges get the token), not Chrome.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.browser import ChromeBrowserTool


class FakeCdpSession:
    def __init__(self):
        self.handlers = {}
        self.sent = []
        self.enable_seen_handlers = None

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Fetch.enable":
            self.enable_seen_handlers = set(self.handlers)

    async def emit(self, event, payload):
        self.handlers[event](payload)
        await asyncio.sleep(0)  # let the spawned task run
        await asyncio.sleep(0)


class FakeContext:
    def __init__(self, session):
        self._session = session

    async def new_cdp_session(self, page):
        return self._session


class FakePage:
    def __init__(self, evaluate_result=None, evaluate_error=None):
        self._result = evaluate_result
        self._error = evaluate_error
        self.url = "https://www.tiktok.com/messages"

    def is_closed(self):
        return False

    async def evaluate(self, script):
        if self._error:
            raise self._error
        return self._result


def _tool(**kwargs):
    tool = ChromeBrowserTool(egw_workspace_id="shop-abc", egw_token="tok-1", **kwargs)
    tool._browser = mock.Mock(is_connected=lambda: True)
    return tool


class ProxyAuthHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeCdpSession()
        self.tool = _tool()
        self.tool._context = FakeContext(self.session)
        self.tool._page = FakePage()
        await self.tool._install_proxy_auth()

    def test_both_handlers_are_registered_before_fetch_enable(self):
        self.assertEqual(
            self.session.enable_seen_handlers,
            {"Fetch.authRequired", "Fetch.requestPaused"},
        )

    def test_fetch_enable_asks_for_auth_events(self):
        method, params = self.session.sent[-1]
        self.assertEqual(method, "Fetch.enable")
        self.assertTrue(params["handleAuthRequests"])
        self.assertEqual(params["patterns"], [{"urlPattern": "*"}])

    async def test_proxy_challenge_gets_workspace_credentials(self):
        await self.session.emit("Fetch.authRequired", {
            "requestId": "r1", "authChallenge": {"source": "Proxy", "origin": "http://127.0.0.1:3128"},
        })
        method, params = self.session.sent[-1]
        self.assertEqual(method, "Fetch.continueWithAuth")
        self.assertEqual(params["requestId"], "r1")
        self.assertEqual(params["authChallengeResponse"], {
            "response": "ProvideCredentials", "username": "shop-abc", "password": "tok-1",
        })

    async def test_server_challenge_never_receives_the_token(self):
        """TikTok/Google may answer 401 themselves; those must keep Chrome's
        default handling, or the egw token leaks to a third party."""
        await self.session.emit("Fetch.authRequired", {
            "requestId": "r2", "authChallenge": {"source": "Server", "origin": "https://www.tiktok.com"},
        })
        method, params = self.session.sent[-1]
        self.assertEqual(method, "Fetch.continueWithAuth")
        self.assertEqual(params["authChallengeResponse"], {"response": "Default"})
        self.assertNotIn("tok-1", str(params))

    async def test_every_paused_request_is_continued(self):
        for request_id in ("a", "b", "c"):
            await self.session.emit("Fetch.requestPaused", {"requestId": request_id})
        continued = [p["requestId"] for m, p in self.session.sent if m == "Fetch.continueRequest"]
        self.assertEqual(continued, ["a", "b", "c"])

    def test_disabled_without_both_credentials(self):
        self.assertFalse(ChromeBrowserTool(egw_workspace_id="shop-abc").egw_enabled)
        self.assertFalse(ChromeBrowserTool(egw_token="tok").egw_enabled)
        self.assertFalse(ChromeBrowserTool().egw_enabled)
        self.assertTrue(self.tool.egw_enabled)


class VerifyEgressIpTests(unittest.IsolatedAsyncioTestCase):
    def _tool_with_page(self, page):
        tool = _tool()
        tool._page = page
        tool._context = mock.Mock()
        return tool

    async def test_exact_match_with_expected_ip(self):
        tool = self._tool_with_page(FakePage(evaluate_result="203.0.113.10\n"))
        result = await tool.verify_egress_ip("203.0.113.10")
        self.assertTrue(result["verified"])
        self.assertEqual(result["observed_ip"], "203.0.113.10")

    async def test_mismatch_with_expected_ip_fails_closed(self):
        tool = self._tool_with_page(FakePage(evaluate_result="198.51.100.7"))
        result = await tool.verify_egress_ip("203.0.113.10")
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "observed_ip_differs_from_expected")

    async def test_without_expected_ip_observed_must_differ_from_host(self):
        tool = self._tool_with_page(FakePage(evaluate_result="203.0.113.10"))
        with mock.patch.object(ChromeBrowserTool, "_direct_host_ip", mock.AsyncMock(return_value="198.51.100.1")):
            self.assertTrue((await tool.verify_egress_ip())["verified"])
        with mock.patch.object(ChromeBrowserTool, "_direct_host_ip", mock.AsyncMock(return_value="203.0.113.10")):
            result = await tool.verify_egress_ip()
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "observed_ip_equals_host_ip")

    async def test_unknown_host_ip_is_not_verified(self):
        tool = self._tool_with_page(FakePage(evaluate_result="203.0.113.10"))
        with mock.patch.object(ChromeBrowserTool, "_direct_host_ip", mock.AsyncMock(return_value=None)):
            result = await tool.verify_egress_ip()
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "host_ip_unknown")

    async def test_probe_failure_is_not_verified(self):
        tool = self._tool_with_page(FakePage(evaluate_error=RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")))
        result = await tool.verify_egress_ip("203.0.113.10")
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "probe_failed")


if __name__ == "__main__":
    unittest.main()
