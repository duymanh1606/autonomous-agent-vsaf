"""Who hears what on /api/events.

The live bug this pins: the hub kept one global set of browser queues and
`publish()` fanned every event out to all of them. A logged-in user with no
workspace grants at all received customer message text, escalations and order
changes for *every* shop on the box - and, on connect, a replay of the cached
state of every account before a single new thing had happened. The payloads
already carried `account_id`; nothing read it.

Also covered: the WebSocket handshake is the one path `SameSite=Lax` does not
protect (a page on another origin can open one and the browser attaches the
session cookie), and a grant revoked while a tab is open has to stop mattering
without waiting for the tab to close.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main


ADMIN = {"id": 1, "is_active": True, "is_admin": True}
ALICE = {"id": 2, "is_active": True, "is_admin": False}   # granted acct-a
BOB = {"id": 3, "is_active": True, "is_admin": False}     # granted acct-b

GRANTS = {1: [], 2: ["acct-a"], 3: ["acct-b"]}
ROWS = {1: ADMIN, 2: ALICE, 3: BOB}


class EventIsolationTests(unittest.TestCase):
    def setUp(self):
        # The hub is module state shared with every other test in the process.
        main.hub._latest.clear()
        main.hub._clients.clear()
        self.addCleanup(main.hub._latest.clear)
        self.addCleanup(main.hub._clients.clear)

        self.user_id = ALICE["id"]
        self.grants = dict(GRANTS)
        patches = [
            mock.patch.object(
                main.auth, "decode_session",
                side_effect=lambda _token: (self.user_id, 0),
            ),
            mock.patch.object(
                main.users_store, "get_by_id", side_effect=lambda uid: ROWS.get(uid),
            ),
            mock.patch.object(
                main.users_store, "accessible_account_ids",
                side_effect=lambda uid: list(self.grants.get(uid, [])),
            ),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
            # The notification sink writes to the real database otherwise, and
            # it deliberately sees every event regardless of audience.
            mock.patch.object(main.hub, "on_event", None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(main.auth.SESSION_COOKIE, "probe-token")
        return client

    @staticmethod
    def _drain(socket, expected_types=1):
        """Read frames until the heartbeat, returning the real events."""
        events = []
        for _ in range(expected_types + 3):
            frame = socket.receive_json()
            if frame.get("type") == "heartbeat":
                break
            events.append(frame)
            if len(events) >= expected_types:
                break
        return events

    # ------------------------------------------------------------- live
    def test_a_user_hears_their_own_workspace_and_not_the_other_one(self):
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.2):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    main.hub.emit("acct-b", "agent.status", status="running")
                    main.hub.emit("acct-a", "agent.status", status="running")
                    events = self._drain(socket)

        self.assertEqual([event["account_id"] for event in events], ["acct-a"])

    def test_an_admin_hears_everything(self):
        self.user_id = ADMIN["id"]
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.2):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    main.hub.emit("acct-a", "agent.status", status="running")
                    main.hub.emit("acct-b", "agent.status", status="running")
                    events = self._drain(socket, expected_types=2)

        self.assertEqual(
            [event["account_id"] for event in events], ["acct-a", "acct-b"],
        )

    def test_an_event_without_an_account_reaches_admins_only(self):
        """Fail-closed: an emit that forgot its account id must not become a
        broadcast. `orchestrator` had exactly one such call site."""
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.2):
            with self._client() as client:                    # Alice
                with client.websocket_connect("/api/events") as socket:
                    main.hub.publish({"account_id": "", "type": "runplan.finished"})
                    main.hub.emit("acct-a", "agent.status", status="running")
                    events = self._drain(socket)
        self.assertEqual([event["type"] for event in events], ["agent.status"])

        # `agent.status` is a REPLAY_TYPE, so it is still in the hub's cache
        # and would be the admin's first frame below - clear it so the next
        # assertion is about the account-less event and nothing else.
        main.hub._latest.clear()
        self.user_id = ADMIN["id"]
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.2):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    main.hub.publish({"account_id": "", "type": "runplan.finished"})
                    events = self._drain(socket)
        self.assertEqual([event["type"] for event in events], ["runplan.finished"])

    # ----------------------------------------------------------- replay
    def test_the_connect_replay_is_filtered_too(self):
        """The worst of the leak: one event per account, before the new tab
        had done anything at all."""
        main.hub.emit("acct-a", "agent.status", status="running")
        main.hub.emit("acct-b", "agent.status", status="running")

        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.2):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    events = self._drain(socket)

        self.assertEqual([event["account_id"] for event in events], ["acct-a"])

    # ------------------------------------------------------- revocation
    def test_a_revoked_grant_stops_delivery_within_one_heartbeat(self):
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.05):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    main.hub.emit("acct-a", "agent.status", status="running")
                    self.assertEqual(self._drain(socket)[0]["account_id"], "acct-a")

                    # An admin revokes the grant while the tab is open.
                    self.grants[ALICE["id"]] = []
                    # One heartbeat is what the handler needs to notice.
                    self.assertEqual(socket.receive_json()["type"], "heartbeat")

                    main.hub.emit("acct-a", "agent.status", status="stopped")
                    self.assertEqual(socket.receive_json()["type"], "heartbeat")

    def test_a_deactivated_user_is_dropped_mid_connection(self):
        """An admin deactivating someone must not have to wait for that
        person to close their tab.

        Asserted through `hub._clients` rather than by reading the socket to
        its close frame: the handler ending is the behaviour under test, and
        driving the client side of a half-closed TestClient WebSocket hangs
        on its own teardown.
        """
        deactivated = dict(ALICE, is_active=False)
        with mock.patch.object(main, "EVENTS_HEARTBEAT_SECONDS", 0.05):
            with self._client() as client:
                with client.websocket_connect("/api/events") as socket:
                    main.hub.emit("acct-a", "agent.status", status="running")
                    self.assertEqual(self._drain(socket)[0]["account_id"], "acct-a")
                    self.assertEqual(len(main.hub._clients), 1)

                    with mock.patch.object(
                        main.users_store, "get_by_id", return_value=deactivated,
                    ):
                        deadline = time.monotonic() + 3
                        while main.hub._clients and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertEqual(
                            main.hub._clients, {},
                            "the handler kept the connection after deactivation",
                        )

    # ----------------------------------------------------- handshake
    def test_a_cross_site_upgrade_is_refused_once_origins_are_configured(self):
        from starlette.websockets import WebSocketDisconnect

        with mock.patch.dict(
            "os.environ", {"DASHBOARD_TRUSTED_ORIGINS": "https://app.example.com"},
        ):
            with self._client() as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        "/api/events", headers={"origin": "https://evil.test"},
                    ):
                        pass

                # The real origin still gets in.
                with client.websocket_connect(
                    "/api/events", headers={"origin": "https://app.example.com"},
                ) as socket:
                    main.hub.emit("acct-a", "agent.status", status="running")
                    self.assertEqual(socket.receive_json()["account_id"], "acct-a")

    def test_a_revoked_session_cannot_open_a_socket(self):
        from datetime import datetime, timedelta, timezone
        from starlette.websockets import WebSocketDisconnect

        revoked = dict(
            ALICE, sessions_valid_from=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with mock.patch.object(main.users_store, "get_by_id", return_value=revoked):
            with self._client() as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/api/events"):
                        pass


if __name__ == "__main__":
    unittest.main()
