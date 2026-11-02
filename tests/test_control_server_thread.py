"""The control surface must answer while the agent is busy.

It used to share the agent's event loop, so every operator action queued
behind whatever the agent was doing - including steps that do not yield at
all, such as probing a video with `ffprobe` (capped at 5s). Taking over a
conversation is two indexed queries; waiting seconds for it was waiting on the
wrong thing.

The server now runs on its own loop in its own thread. These tests pin the
three properties that makes worth having, and the one it must not break:

* an operator's call is served while the agent's loop is blocked solid;
* calls that touch the agent's own objects still run on the agent's loop, and
  say so instead of hanging when the agent never yields;
* with no agent loop attached, everything runs inline exactly as before -
  which is what every other test in this suite exercises.
"""

import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from control.auth import TOKEN_HEADER
from control.events import EventBus
from control.server import ControlServer
from control.state import RuntimeStatus


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _isolate_token_dir(test):
    # start() publishes the token file; keep it out of the repo's data/.
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    patcher = mock.patch.dict("os.environ", {"AGENT_CONTROL_TOKEN_DIR": directory.name})
    patcher.start()
    test.addCleanup(patcher.stop)


class ControlServerThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _isolate_token_dir(self)
        self.state = SimpleNamespace(
            running=True, paused=False, pause_reason="", current_task_id=None,
        )
        self.status = RuntimeStatus(
            account_id="acc_test", runtime="langgraph",
            agent=SimpleNamespace(state=self.state),
            health_probe=self._health,
        )
        self.health_calls = []
        self.port = _free_port()
        self.server = ControlServer(
            self.status, EventBus("acc_test"), port=self.port,
            shutdown=self._shutdown,
        )
        self.stopped_on = []
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)

    async def _health(self):
        # Records which loop it ran on: the point is that it is the agent's.
        self.health_calls.append(asyncio.get_running_loop())
        return {"probe": {"ok": True}}

    def _shutdown(self):
        self.stopped_on.append(threading.current_thread().name)

    async def _get(self, path, timeout=5.0):
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(
                f"http://127.0.0.1:{self.port}{path}", headers=self._auth(),
            )

    async def _post(self, path, payload=None, timeout=5.0):
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"http://127.0.0.1:{self.port}{path}", json=payload,
                headers=self._auth(),
            )

    def _auth(self):
        return {TOKEN_HEADER: self.server.token}

    async def test_it_serves_on_its_own_thread(self):
        response = await self._get("/control/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_id"], "acc_test")
        self.assertIsNotNone(self.server._thread)
        self.assertNotEqual(
            self.server._thread.name, threading.current_thread().name
        )

    async def test_an_operator_is_served_while_the_agent_loop_is_blocked(self):
        """The regression this whole change is for. `time.sleep` in a task on
        the agent's loop is exactly what `subprocess.run(ffprobe)` does to it."""
        blocked = asyncio.get_running_loop().run_in_executor(None, lambda: None)
        await blocked

        def block_the_agent_loop():
            time.sleep(1.5)

        # Scheduled on this test's loop, which is the agent loop the server
        # captured in start(): nothing else on it can run for 1.5s.
        asyncio.get_running_loop().call_soon(block_the_agent_loop)
        # Let the callback begin before the request goes out.
        await asyncio.sleep(0)

        started = time.monotonic()
        status = await asyncio.to_thread(
            lambda: httpx.post(
                f"http://127.0.0.1:{self.port}/control/pause",
                json={"reason": "operator"}, timeout=5.0, headers=self._auth(),
            )
        )
        elapsed = time.monotonic() - started

        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["paused"])
        # Served during the block, not after it.
        self.assertLess(elapsed, 1.4)

    async def test_health_still_runs_on_the_agent_loop(self):
        """It probes the agent's browser and database connection, so running
        it anywhere else would touch those from the wrong thread."""
        agent_loop = asyncio.get_running_loop()

        response = await asyncio.to_thread(
            lambda: httpx.get(
                f"http://127.0.0.1:{self.port}/control/health", timeout=5.0,
                headers=self._auth(),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.health_calls, [agent_loop])

    async def test_shutdown_is_handed_to_the_agent_loop(self):
        """Its `asyncio.Event` belongs to that loop, so calling it from the
        server thread would be a data race on asyncio internals."""
        await asyncio.to_thread(
            lambda: httpx.post(
                f"http://127.0.0.1:{self.port}/control/shutdown", timeout=5.0,
                headers=self._auth(),
            )
        )
        # The callback is scheduled, not called inline: give the loop a turn.
        await asyncio.sleep(0.05)

        self.assertEqual(self.stopped_on, [threading.current_thread().name])

    async def test_drain_reports_from_the_server_thread_without_the_agent(self):
        self.state.current_task_id = None

        body = (await self._post("/control/drain", {"timeout_seconds": 2})).json()

        self.assertTrue(body["drained"])
        self.assertTrue(self.state.paused)


class AgentLoopTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """An agent that never yields must produce an answer, not a hung socket."""

    async def test_a_blocked_agent_loop_becomes_a_504(self):
        _isolate_token_dir(self)
        state = SimpleNamespace(
            running=True, paused=False, pause_reason="", current_task_id=None,
        )

        async def never_finishes():
            await asyncio.Event().wait()

        status = RuntimeStatus(
            account_id="acc_test", runtime="langgraph",
            agent=SimpleNamespace(state=state), health_probe=never_finishes,
        )
        server = ControlServer(status, EventBus("acc_test"), port=_free_port())
        await server.start()
        self.addAsyncCleanup(server.stop)

        response = await asyncio.to_thread(
            lambda: httpx.get(
                f"http://127.0.0.1:{server.port}/control/health", timeout=10.0,
                headers={TOKEN_HEADER: server.token},
            )
        )

        self.assertEqual(response.status_code, 504)
        self.assertIn("cannot be interrupted", response.json()["detail"])

    async def asyncSetUp(self):
        # Keep the timeout short enough for a test; the production value is
        # sized for the agent's longest uninterruptible step.
        import control.server as module

        self._original = module.AGENT_CALL_TIMEOUT_SECONDS
        module.AGENT_CALL_TIMEOUT_SECONDS = 0.5

        def restore():
            module.AGENT_CALL_TIMEOUT_SECONDS = self._original

        self.addCleanup(restore)


if __name__ == "__main__":
    unittest.main()
