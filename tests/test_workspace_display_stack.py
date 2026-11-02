"""Startup/shutdown order for a workspace's own Xvfb/x11vnc/noVNC trio.

Each workspace used to share one always-on Xvfb/x11vnc/noVNC with every other
workspace on the box - anyone who opened VNC saw and could click into every
workspace's Chrome, not just the one they were authorized for. Each workspace
now gets its own dedicated instance of all three, spawned and torn down by
`AccountProcesses` alongside Chrome, exactly like Chrome and the agent already
were.

The one thing that must never regress: the display has to exist before
anything tries to attach to it, and Chrome has to be last (it is the actual
client of the other three). `sleep_chrome()`/wake, in contrast, must NEVER
touch this trio - a workspace put to sleep only stops Chrome, so an operator
watching its noVNC tab sees a blank screen rather than a dropped connection
(see dashboard_api/sleep_wake.py, deliberately not touched by this change).
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.processes as processes


def _account(account_id="ws-1"):
    return SimpleNamespace(
        id=account_id,
        label=account_id,
        profile_dir=Path(f"/profiles/{account_id}"),
        config_path=Path(f"/configs/{account_id}.yaml"),
        cdp_port=29222,
        display_num=100,
        vnc_port=25901,
        novnc_port=26081,
        api_key="",
    )


class EnsureXauthTests(unittest.TestCase):
    """The one piece of this change with no prior precedent in this codebase:
    generating an X11 authority cookie without a live X server to talk to
    (the same technique startx/xinit use - `xauth add`, not relying on Xvfb
    to create the file itself). Runs the real `xauth` binary, not a mock -
    this is exactly the kind of "should work" assumption that bit
    dashboard_api/processes.py before (see the profile-reset comment in
    start_chrome)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = _account(account_id="ws-xauth")
        patcher = mock.patch.object(processes, "BASE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.managed = processes.AccountProcesses(self.account, "langgraph")

    @unittest.skipIf(shutil.which("xauth") is None, "xauth not installed")
    def test_generates_a_real_cookie_file(self):
        self.managed._ensure_xauth()

        path = self.managed._xauth_path()
        self.assertTrue(path.exists())
        # 0600: this cookie authenticates every client of the workspace's own
        # display (Chrome, x11vnc) - it must not be group/world readable.
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        listing = subprocess.run(
            ["xauth", "-f", str(path), "list"], capture_output=True, text=True,
        )
        self.assertIn(f":{self.account.display_num}", listing.stdout)
        self.assertIn("MIT-MAGIC-COOKIE-1", listing.stdout)

    @unittest.skipIf(shutil.which("xauth") is None, "xauth not installed")
    def test_is_idempotent(self):
        """A supervisor restart must never invalidate a display that's still
        up with clients already holding the old cookie."""
        self.managed._ensure_xauth()
        path = self.managed._xauth_path()
        first_cookie = path.read_bytes()

        self.managed._ensure_xauth()

        self.assertEqual(path.read_bytes(), first_cookie)

    def test_missing_xauth_binary_is_a_warning_not_a_crash(self):
        with mock.patch.object(shutil, "which", return_value=None):
            self.managed._ensure_xauth()  # must not raise
        self.assertFalse(self.managed._xauth_path().exists())


class StartOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order: list[str] = []
        self.managed = processes.AccountProcesses(_account(), "langgraph")

        # Record which ManagedProcess.start()/.stop() fired, in order, without
        # actually spawning anything - each ManagedProcess already knows its
        # own name (f"{account.id}:xvfb" etc, set in AccountProcesses.__init__).
        def recording_start(managed_process):
            self.order.append(f"start:{managed_process.name}")

        def recording_stop(managed_process):
            self.order.append(f"stop:{managed_process.name}")
            return "stopped"

        for managed_process in (
            self.managed.xvfb, self.managed.x11vnc, self.managed.novnc,
            self.managed.chrome, self.managed.agent,
        ):
            managed_process.start = AsyncMock(
                side_effect=lambda mp=managed_process: recording_start(mp)
            )
            managed_process.stop = AsyncMock(
                side_effect=lambda mp=managed_process: recording_stop(mp)
            )

        patches = [
            mock.patch.object(processes, "_repack_extension_if_needed", AsyncMock()),
            mock.patch.object(processes, "_find_running_pid", return_value=None),
            mock.patch.object(processes, "_reset_stale_extension_profile"),
            mock.patch.object(processes.asyncio, "sleep", AsyncMock()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_the_display_stack_comes_up_before_chrome(self):
        await self.managed.start_chrome()

        self.assertEqual(
            self.order,
            ["start:ws-1:xvfb", "start:ws-1:x11vnc", "start:ws-1:novnc", "start:ws-1:chrome"],
        )

    async def test_stop_all_tears_down_in_reverse_with_agent_first(self):
        await self.managed.start_chrome()
        self.order.clear()

        await self.managed.stop_all(include_chrome=True)

        self.assertEqual(
            self.order,
            [
                "stop:ws-1:agent", "stop:ws-1:chrome",
                "stop:ws-1:novnc", "stop:ws-1:x11vnc", "stop:ws-1:xvfb",
            ],
        )

    async def test_stop_all_without_chrome_leaves_the_display_stack_up(self):
        """The historical `include_chrome=False` escape hatch (an operator
        with a specific reason to keep the browser open) - the display stack
        exists only to serve Chrome, so it follows Chrome's own fate here."""
        await self.managed.start_chrome()
        self.order.clear()

        await self.managed.stop_all(include_chrome=False)

        self.assertEqual(self.order, ["stop:ws-1:agent"])

    async def test_sleeping_never_touches_the_display_stack(self):
        """sleep_chrome() is Chrome-only by design (dashboard_api/sleep_wake.py
        pauses the agent over HTTP instead of stopping it) - an open noVNC tab
        must keep its connection through a sleep, showing a blank screen
        rather than dropping."""
        await self.managed.start_chrome()
        self.order.clear()

        await self.managed.sleep_chrome()

        self.assertEqual(self.order, ["stop:ws-1:chrome"])


if __name__ == "__main__":
    unittest.main()
