"""Reconciling the supervisor's process table with reality at boot.

Agents are started detached on purpose so they outlive the supervisor, but
`processes` is rebuilt empty on every boot. Until the sweep these tests cover,
a restart left every still-running workspace reading as "Stopped" on the
dashboard - the state deploy/README.md warns about, and the same blind spot
let the extension-profile reset fire at a live Chrome.

The rule the sweep must never break: it adopts, it never starts.
"""

import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.processes as processes


def account(account_id="acct-1"):
    return SimpleNamespace(
        id=account_id,
        label=account_id,
        profile_dir=Path(f"/profiles/{account_id}"),
        config_path=Path(f"/configs/{account_id}.yaml"),
        cdp_port=29222,
        # Each workspace now has its own display stack (AccountProcesses
        # constructs xvfb/x11vnc/novnc alongside chrome/agent) - these three
        # are needed just to build AccountProcesses, not exercised directly
        # by anything in this file.
        display_num=100,
        vnc_port=25901,
        novnc_port=26081,
        api_key="",
    )


class AdoptIfRunningTests(unittest.TestCase):
    def _managed(self):
        return processes.ManagedProcess("acct-1:agent", ["python3", "/configs/acct-1.yaml"])

    def test_a_live_orphan_is_claimed(self):
        managed = self._managed()
        with mock.patch.object(processes, "_find_running_pid", return_value=4242):
            self.assertEqual(managed.adopt_if_running(), 4242)
        with mock.patch.object(processes, "_pid_alive", return_value=True):
            self.assertTrue(managed.running)
            self.assertEqual(managed.pid, 4242)

    def test_nothing_running_leaves_the_slot_empty(self):
        managed = self._managed()
        with mock.patch.object(processes, "_find_running_pid", return_value=None):
            self.assertIsNone(managed.adopt_if_running())
        self.assertFalse(managed.running)
        self.assertIsNone(managed.pid)

    def test_adopting_never_spawns_anything(self):
        """The whole point: a boot sweep must not resurrect a workspace an
        operator deliberately stopped."""
        managed = self._managed()
        with (
            mock.patch.object(processes, "_find_running_pid", return_value=None),
            mock.patch.object(processes.asyncio, "create_subprocess_exec") as spawn,
        ):
            managed.adopt_if_running()
        spawn.assert_not_called()

    def test_a_process_this_supervisor_started_is_left_alone(self):
        managed = self._managed()
        managed.process = SimpleNamespace(pid=99, returncode=None)
        with mock.patch.object(processes, "_find_running_pid") as lookup:
            self.assertEqual(managed.adopt_if_running(), 99)
        lookup.assert_not_called()


class FindRunningPidPathMatchingTests(unittest.TestCase):
    """`_find_running_pid` only ever matches a path-like ("/"-containing)
    argument (processes.py's own docstring on it). Chrome/agent always have
    one for free (--user-data-dir/--config); Xvfb/x11vnc/websockify's own
    flags (-display, -rfbport, host:port pairs) don't, which is exactly why
    _xvfb_command/_x11vnc_command/_novnc_command each add one real,
    account-unique path argument (-auth/-o/--log-file) - these tests prove
    that choice is load-bearing, against the real function, not a mock.
    """

    def setUp(self):
        self.processes = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for proc in self.processes:
            proc.kill()
            proc.wait(timeout=5)

    def _spawn_with_trailing_arg(self, marker: str) -> None:
        """A real, findable process whose full command line contains `marker`
        as its last argument - Python itself never looks at it, but `pgrep -f`
        sees it in /proc/<pid>/cmdline the same way it would see a real
        -auth/-o/--log-file value."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", marker],
        )
        self.processes.append(proc)
        # pgrep reads a freshly-exec'd process's cmdline from /proc; give it
        # a moment to be visible rather than racing the spawn.
        time.sleep(0.3)

    def test_a_path_like_argument_is_found(self):
        marker = f"/tmp/find-pid-test-{id(self)}-with-slash"
        self._spawn_with_trailing_arg(marker)

        found = processes._find_running_pid(["Xvfb", ":150", "-auth", marker])

        self.assertEqual(found, self.processes[0].pid)

    def test_no_path_like_argument_is_never_found(self):
        """The exact failure mode a bare Xvfb/x11vnc/websockify command has
        without -auth/-o/--log-file: nothing in the command line contains a
        "/", so _find_running_pid gives up before it ever calls pgrep -
        confirmed here by a marker that IS running but has no slash in it."""
        marker = f"find-pid-test-{id(self)}-no-slash"
        self._spawn_with_trailing_arg(marker)

        found = processes._find_running_pid(["Xvfb", ":150", "-screen", "0", marker])

        self.assertIsNone(found)


class DetectAgentRuntimeTests(unittest.TestCase):
    """What the dashboard reports for a workspace whose agent it adopted.

    There used to be two runtimes sharing one config path, and the running
    process was the only record of which entry point had been used - so this
    read the command line to tell them apart. The extension runtime has since
    been removed (nothing could select it: its picker sat behind
    `SHOW_TECHNICAL_INFO = false`), leaving one answer. What still matters is
    the distinction between *running* and *not running at all*.
    """

    def test_a_running_agent_reports_the_one_runtime(self):
        with (
            mock.patch.object(processes, "_find_running_pid", return_value=7),
            mock.patch.object(processes, "_process_cmdline",
                              return_value="python3 main.py --config /configs/acct-1.yaml"),
        ):
            self.assertEqual(processes.detect_agent_runtime(account()), "langgraph")

    def test_no_agent_running_reports_nothing(self):
        with mock.patch.object(processes, "_find_running_pid", return_value=None):
            self.assertIsNone(processes.detect_agent_runtime(account()))

    def test_an_unreadable_cmdline_falls_back_to_the_default(self):
        """A process that exited between the lookup and the read must not
        crash the sweep."""
        with (
            mock.patch.object(processes, "_find_running_pid", return_value=7),
            mock.patch.object(processes, "_process_cmdline", return_value=""),
        ):
            self.assertEqual(processes.detect_agent_runtime(account()), "langgraph")


class AdoptRunningAllProcessesTests(unittest.TestCase):
    """`adopt_running()` now walks all five of a workspace's processes -
    xvfb, x11vnc, novnc, chrome, agent, in that order (see AccountProcesses.
    adopt_running) - not just the chrome/agent pair this class was written
    for before each workspace had its own display stack."""

    def _managed(self):
        return processes.AccountProcesses(account(), "langgraph")

    def test_everything_is_claimed(self):
        managed = self._managed()
        with mock.patch.object(processes, "_find_running_pid",
                               side_effect=[100, 101, 102, 11, 22]):
            found = managed.adopt_running()
        self.assertEqual(
            found, {"xvfb": 100, "x11vnc": 101, "novnc": 102, "chrome": 11, "agent": 22},
        )

    def test_chrome_alone_is_reported_as_chrome_alone(self):
        """A workspace whose agent crashed but whose Chrome (and the display
        stack it needs) is still up is a real state, and reporting it as
        fully stopped hides the crash."""
        managed = self._managed()
        with mock.patch.object(processes, "_find_running_pid",
                               side_effect=[100, 101, 102, 11, None]):
            found = managed.adopt_running()
        self.assertEqual(found, {"xvfb": 100, "x11vnc": 101, "novnc": 102, "chrome": 11})

    def test_a_stopped_workspace_reports_nothing(self):
        managed = self._managed()
        with mock.patch.object(processes, "_find_running_pid", return_value=None):
            self.assertEqual(managed.adopt_running(), {})


class BootSweepTests(unittest.TestCase):
    """`adopt_running_processes` in dashboard_api.main."""

    def setUp(self):
        import dashboard_api.main as main
        self.main = main
        self.accounts = [account("acct-1"), account("acct-2")]
        patcher = mock.patch.object(
            main, "registry", SimpleNamespace(list=lambda: self.accounts),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        main.processes.clear()
        self.addCleanup(main.processes.clear)

    def test_only_accounts_with_something_alive_enter_the_table(self):
        """An account with nothing running is deliberately left out, so the
        sweep cannot change how a genuinely stopped workspace reports."""
        def only_acct_1(command):
            return 100 if "acct-1" in " ".join(str(part) for part in command) else None

        with (
            mock.patch.object(processes, "_find_running_pid", side_effect=only_acct_1),
            mock.patch.object(processes, "_process_cmdline", return_value="main.py"),
        ):
            adopted = self.main.adopt_running_processes()

        self.assertEqual(set(adopted), {"acct-1"})
        self.assertEqual(set(self.main.processes), {"acct-1"})

    def test_an_account_that_raises_does_not_stop_the_others(self):
        calls = {"n": 0}

        def explode_on_first(command):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pgrep blew up")
            return 200

        with (
            mock.patch.object(processes, "_find_running_pid", side_effect=explode_on_first),
            mock.patch.object(processes, "_process_cmdline", return_value="main.py"),
        ):
            adopted = self.main.adopt_running_processes()

        self.assertIn("acct-2", adopted)

    def test_the_sweep_never_runs_twice_over_an_account(self):
        """Something already in the table was put there by this supervisor;
        re-adopting would overwrite a live handle with a bare pid."""
        sentinel = object()
        self.main.processes["acct-1"] = sentinel

        with (
            mock.patch.object(processes, "_find_running_pid", return_value=300),
            mock.patch.object(processes, "_process_cmdline", return_value="main.py"),
        ):
            self.main.adopt_running_processes()

        self.assertIs(self.main.processes["acct-1"], sentinel)


if __name__ == "__main__":
    unittest.main()
