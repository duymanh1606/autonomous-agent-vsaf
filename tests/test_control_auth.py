"""The control server answers only callers holding its token.

It listens on loopback, which every local process - and every web page open
in a browser on this host - can reach. Before this check it also allowed any
origin over CORS, so a page could submit a send_message task to a customer or
read customer text off /control/events. These tests pin down the three things
that closed that: no token no answer, no CORS for a browser to lean on, and a
supervisor that still gets in (including after it restarts and adopts a
running agent, which is why the token lives in a file).
"""

import asyncio
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

from control import auth
from control.events import EventBus
from control.server import ControlServer
from control.state import RuntimeStatus


def _server(**kwargs):
    state = SimpleNamespace(
        running=True, paused=False, pause_reason="", current_task_id=None,
    )
    status = RuntimeStatus(
        account_id="acc_test", runtime="langgraph",
        agent=SimpleNamespace(state=state),
    )
    submitted = []

    async def submit(**task):
        submitted.append(task)
        return "task-1"

    server = ControlServer(
        status, EventBus("acc_test"), task_submitter=submit, **kwargs
    )
    return server, submitted


SEND = {"capability": "send_message",
        "payload": {"recipient": "khach", "response": "chuyen khoan vao ..."}}


class TokenRequiredTests(unittest.TestCase):
    def setUp(self):
        self.server, self.submitted = _server()
        self.client = TestClient(self.server.app)

    def test_every_route_refuses_a_caller_without_the_token(self):
        for method, path, body in [
            ("POST", "/control/tasks", SEND),
            ("POST", "/control/shutdown", None),
            ("POST", "/control/pause", {}),
            ("POST", "/control/drain", {}),
            ("POST", "/control/resume", None),
            ("PATCH", "/control/settings", {"values": {}}),
            ("POST", "/control/conversations/c-1/takeover", {}),
            ("POST", "/control/conversations/c-1/release", None),
            ("GET", "/control/status", None),
            ("GET", "/control/health", None),
            ("GET", "/control/events", None),
        ]:
            with self.subTest(route=f"{method} {path}"):
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.submitted, [])

    def test_a_wrong_token_is_refused(self):
        response = self.client.post(
            "/control/tasks", json=SEND, headers={auth.TOKEN_HEADER: "guess"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.submitted, [])

    def test_the_right_token_is_served(self):
        response = self.client.post(
            "/control/tasks", json=SEND,
            headers={auth.TOKEN_HEADER: self.server.token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.submitted), 1)

    def test_a_server_built_without_a_token_is_locked_not_open(self):
        self.assertTrue(self.server.token)
        other, _ = _server()
        self.assertNotEqual(self.server.token, other.token)


class NoCorsTests(unittest.TestCase):
    """A page on another origin must get nothing a browser would act on."""

    def setUp(self):
        self.server, self.submitted = _server()
        self.client = TestClient(self.server.app)

    def test_a_cross_origin_preflight_is_not_granted(self):
        response = self.client.options("/control/tasks", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertNotIn("access-control-allow-methods", response.headers)

    def test_the_event_stream_is_not_readable_cross_origin(self):
        response = self.client.get(
            "/control/status", headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access-control-allow-origin", response.headers)


class TokenFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name) / "control"
        patcher = mock.patch.dict(os.environ, {"AGENT_CONTROL_TOKEN_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_start_publishes_the_token_privately_and_stop_withdraws_it(self):
        server, _ = _server(port=_free_port())
        await server.start()
        path = auth.token_path(server.port)
        try:
            self.assertEqual(auth.read_token(server.port), server.token)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

            # What the supervisor does: read the file, call with it.
            response = await asyncio.to_thread(
                httpx.get, f"http://127.0.0.1:{server.port}/control/status",
                headers=auth.headers_for(server.port), timeout=5.0,
            )
            self.assertEqual(response.status_code, 200)
        finally:
            await server.stop()
        self.assertFalse(path.exists())

    async def test_stopping_never_deletes_a_newer_agents_token(self):
        auth.write_token(28950, "newer-agent")
        auth.remove_token(28950, "older-agent")
        self.assertEqual(auth.read_token(28950), "newer-agent")

    async def test_no_file_means_no_header(self):
        self.assertEqual(auth.headers_for(28951), {})


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_agent_401_is_not_passed_to_the_browser_as_a_logout(self):
        """The dashboard treats 401 as "your session expired". The agent
        refusing the supervisor's token is a gateway problem, not that."""
        from fastapi import HTTPException

        from dashboard_api import main

        class Refusing:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, json=None, headers=None):
                return httpx.Response(
                    401, json={"detail": "missing or wrong control token"},
                    request=httpx.Request(method, url),
                )

        account = SimpleNamespace(id="acc_test", control_port=28952)
        with mock.patch.object(main.httpx, "AsyncClient", Refusing):
            with self.assertLogs("dashboard_api.main", level="WARNING"):
                with self.assertRaises(HTTPException) as raised:
                    await main._control(account, "GET", "/control/status")

        self.assertEqual(raised.exception.status_code, 502)

    async def test_the_supervisor_sends_the_published_token(self):
        from dashboard_api import main

        seen = {}

        class Recording:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, json=None, headers=None):
                seen.update(headers or {})
                return httpx.Response(200, json={}, request=httpx.Request(method, url))

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AGENT_CONTROL_TOKEN_DIR": directory}):
                auth.write_token(28953, "published")
                with mock.patch.object(main.httpx, "AsyncClient", Recording):
                    await main._control(
                        SimpleNamespace(id="acc_test", control_port=28953),
                        "GET", "/control/status",
                    )

        self.assertEqual(seen, {auth.TOKEN_HEADER: "published"})


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
