"""The /api/events handler must never be a task uvicorn waits on forever.

uvicorn's graceful stop waits for every in-flight handler *before* it runs
the lifespan shutdown - so the wake-up sentinel `hub.close()` posts arrives
after the wait it was meant to end. A handler parked indefinitely on
`queue.get()` therefore deadlocked every `systemctl stop` with a dashboard
tab open until systemd SIGKILLed the process 90s later (confirmed live, and
reproduced: >100s with the old loop, 4.5s with this one).

These pin the two properties that fix it: the handler wakes on a timer and
touches the socket, and a socket that has gone away ends the handler rather
than raising out of it.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main


def _authed_client():
    client = TestClient(main.app)
    # The handler checks the session cookie by hand; give it one that
    # resolves to an active user without touching the real user store.
    client.cookies.set(main.auth.SESSION_COOKIE, "probe-token")
    return client


class EventsWebSocketTests(unittest.TestCase):
    def setUp(self):
        patches = [
            # `decode_session` (not `decode_session_token`) is what the auth
            # middleware and the /api/events handler read: it returns
            # `(user_id, issued_at)`, the second half being what a revocation
            # check compares against. issued_at=0 with a row that carries no
            # `sessions_valid_from` means "never revoked".
            mock.patch.object(main.auth, "decode_session", return_value=(1, 0)),
            # An admin, so the handler's per-account filter lets every event
            # through: what these tests pin is the shutdown/heartbeat
            # behaviour, not who may see what (that is
            # tests/test_events_account_isolation.py).
            mock.patch.object(main.users_store, "get_by_id",
                              return_value={"id": 1, "is_active": True,
                                            "is_admin": True}),
            # The handler reads the grant list on connect and refreshes it on
            # every heartbeat; unpatched, that is a real database connection.
            mock.patch.object(main.users_store, "accessible_account_ids",
                              return_value=[]),
            # Lifespan side effects (adopting processes, sweepers) are not
            # under test and must not run against the real machine.
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_quiet_connection_still_gets_a_heartbeat(self):
        """With no events at all the handler must not sit silent forever:
        the timed wake-up is what lets it notice a closing socket.

        The receive runs under a deadline on purpose: the bug this guards
        against is a handler that never sends, and a test that then blocks
        forever is a hung CI run, not a red one."""
        import threading
        result = {}

        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.05):
            with _authed_client() as client:
                with client.websocket_connect("/api/events") as ws:
                    worker = threading.Thread(
                        target=lambda: result.setdefault("frame", ws.receive_text()),
                        daemon=True,
                    )
                    worker.start()
                    worker.join(timeout=3)
                    self.assertFalse(
                        worker.is_alive(),
                        "no heartbeat within 3s - the handler is parked on queue.get() forever",
                    )
        self.assertEqual(result.get("frame"), main.HEARTBEAT_FRAME)

    def test_events_still_flow_between_heartbeats(self):
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 5):
            with _authed_client() as client:
                with client.websocket_connect("/api/events") as ws:
                    main.hub.emit("acct-1", "agent.status", status="running")
                    frame = ws.receive_json()
        self.assertEqual(frame["type"], "agent.status")

    def test_the_shutdown_sentinel_ends_the_handler_cleanly(self):
        """`hub.close()` posts None; the loop must treat it as "stop", not as
        an event to serialise. Driven on one event loop with a stub socket:
        asyncio queues are not thread-safe, so poking one from the
        TestClient's other thread proves nothing."""
        sent = []

        class Socket:
            cookies = {main.auth.SESSION_COOKIE: "probe-token"}
            # A real WebSocket always has these; the handler reads them for
            # the Cloudflare Access and Origin checks before it accepts.
            headers: dict = {}
            async def accept(self): pass
            async def close(self, code=1000): pass
            async def send_text(self, text): sent.append(text)

        async def run():
            handler = asyncio.create_task(main.events(Socket()))
            await asyncio.sleep(0.05)          # handler is parked on queue.get()
            await main.hub.close()             # the real shutdown path
            await asyncio.wait_for(handler, timeout=2)

        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 5):
            asyncio.run(run())

        # Ended on the sentinel - and never serialised it as "null".
        self.assertNotIn("null", sent)

    def test_the_handler_unsubscribes_however_it_ends(self):
        """A queue left behind by a dead tab fills up and then drops every
        later event for that slot; the finally-block is what prevents it."""
        before = len(main.hub._clients)
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 5):
            with _authed_client() as client:
                with client.websocket_connect("/api/events"):
                    self.assertEqual(len(main.hub._clients), before + 1)
        self.assertEqual(len(main.hub._clients), before)


class GracefulShutdownCeilingTests(unittest.TestCase):
    def test_uvicorn_is_given_a_hard_ceiling(self):
        """Without `timeout_graceful_shutdown` uvicorn waits forever for open
        connections and systemd ends up SIGKILLing - skipping the lifespan
        shutdown entirely."""
        with mock.patch.object(main.uvicorn, "run") as run:
            main.main()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("timeout_graceful_shutdown"), main.GRACEFUL_SHUTDOWN_SECONDS)
        # The heartbeat must fire well inside the ceiling, or the ceiling is
        # what always ends the connection and the heartbeat is decoration.
        self.assertLess(main.EVENTS_HEARTBEAT_SECONDS * 1.5, main.GRACEFUL_SHUTDOWN_SECONDS)


if __name__ == "__main__":
    unittest.main()
