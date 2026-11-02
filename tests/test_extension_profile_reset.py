"""Chrome profile hygiene for the DOM bridge extension.

The failure these guard against is not hypothetical: a workspace spent 20+
minutes failing every inbox scan with `[EXT] rejected reason=bridge_unreadable`
because its profile's Preferences still recorded the bridge extension as
installed while the files behind that record were gone. Chrome answers "is it
installed?" from the record, so the forcelist policy never reinstalled it and
no restart could recover. `_reset_stale_extension_profile` must therefore treat
the record and the files as one unit - both checked, both cleared.
"""

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.processes as processes

EXTENSION_ID = "ljgegmjoenegjlhohebndddaaegjebnl"
CURRENT_VERSION = "0.2.1.8"


@dataclass
class _Account:
    id: str
    profile_dir: Path


class ResetStaleExtensionProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        # The packed-extension marker files the function reads to learn which
        # id/version the profile is supposed to end up with.
        id_file = root / "extension_id.txt"
        id_file.write_text(EXTENSION_ID, encoding="utf-8")
        version_file = root / ".version"
        version_file.write_text(CURRENT_VERSION, encoding="utf-8")
        for name, value in (
            ("_EXTENSION_ID_FILE", id_file), ("_EXTENSION_VERSION_FILE", version_file),
        ):
            patcher = mock.patch.object(processes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.profile = root / "profile"
        self.default = self.profile / "Default"
        self.default.mkdir(parents=True)
        self.account = _Account(id="acct-1", profile_dir=self.profile)

    # -- helpers ---------------------------------------------------------

    def _install_files(self, version: str) -> Path:
        """Lay out what Chrome writes when it installs the extension."""
        installed = self.default / "Extensions" / EXTENSION_ID / f"{version}_0"
        installed.mkdir(parents=True)
        (installed / "manifest.json").write_text("{}", encoding="utf-8")
        for name in ("Local Extension Settings", "Extension Rules", "Extension State"):
            (self.default / name / EXTENSION_ID).mkdir(parents=True)
        return installed

    def _write_preferences(self, version: str | None, other_keys: bool = True) -> None:
        settings = {}
        if other_keys:
            settings["aaaaunrelatedextensionidaaaaaaaa"] = {"manifest": {"version": "1.0"}}
        if version is not None:
            settings[EXTENSION_ID] = {
                "manifest": {"version": version},
                "path": f"{EXTENSION_ID}/{version}_0",
                "location": 10,
            }
        payload = {"extensions": {"settings": settings}, "profile": {"name": "Person 1"}}
        (self.default / "Preferences").write_text(json.dumps(payload), encoding="utf-8")

    def _preferences(self) -> dict:
        return json.loads((self.default / "Preferences").read_text(encoding="utf-8"))

    def _settings(self) -> dict:
        return self._preferences()["extensions"]["settings"]

    # -- tests -----------------------------------------------------------

    def test_current_install_is_left_completely_alone(self):
        """The common case - every Chrome start calls this, so a healthy
        profile must survive it untouched, including its stored extension
        data (a wiped `Local Extension Settings` would drop bridge state)."""
        installed = self._install_files(CURRENT_VERSION)
        self._write_preferences(CURRENT_VERSION)

        processes._reset_stale_extension_profile(self.account)

        self.assertTrue((installed / "manifest.json").is_file())
        self.assertTrue((self.default / "Local Extension Settings" / EXTENSION_ID).is_dir())
        self.assertIn(EXTENSION_ID, self._settings())

    def test_stale_version_clears_both_the_files_and_the_record(self):
        """Removing only the files is what created the unrecoverable state;
        the record has to go in the same pass."""
        self._install_files("0.2.1.6")
        self._write_preferences("0.2.1.6")

        processes._reset_stale_extension_profile(self.account)

        self.assertFalse((self.default / "Extensions" / EXTENSION_ID).exists())
        self.assertFalse((self.default / "Local Extension Settings" / EXTENSION_ID).exists())
        self.assertFalse((self.default / "Extension Rules" / EXTENSION_ID).exists())
        self.assertFalse((self.default / "Extension State" / EXTENSION_ID).exists())
        self.assertNotIn(EXTENSION_ID, self._settings())

    def test_record_without_files_is_repaired(self):
        """The exact broken state seen live: Chrome reports the extension as
        installed, nothing is on disk, and the forcelist therefore skips the
        reinstall forever. Nothing else triggers a reset here - only the
        orphaned record - so an earlier `return` on "no Extensions dir" left
        this profile permanently broken."""
        self._write_preferences(CURRENT_VERSION)

        processes._reset_stale_extension_profile(self.account)

        self.assertNotIn(EXTENSION_ID, self._settings())

    def test_repair_keeps_other_extensions_and_the_rest_of_preferences(self):
        """Preferences holds the whole profile; the reset must be a surgical
        key removal, not a file rewrite that loses unrelated settings."""
        self._write_preferences(CURRENT_VERSION)

        processes._reset_stale_extension_profile(self.account)

        self.assertIn("aaaaunrelatedextensionidaaaaaaaa", self._settings())
        self.assertEqual(self._preferences()["profile"]["name"], "Person 1")

    def test_clean_profile_is_a_no_op(self):
        """A brand-new workspace has neither files nor record - already the
        state the forcelist wants, so nothing is rewritten."""
        self._write_preferences(None)
        before = (self.default / "Preferences").read_text(encoding="utf-8")

        processes._reset_stale_extension_profile(self.account)

        self.assertEqual((self.default / "Preferences").read_text(encoding="utf-8"), before)

    def test_files_without_a_record_are_cleared(self):
        """The mirror image of the orphaned record: files Chrome does not know
        about can shadow the install, so they are cleared rather than left."""
        self._install_files(CURRENT_VERSION)
        self._write_preferences(None)

        processes._reset_stale_extension_profile(self.account)

        self.assertFalse((self.default / "Extensions" / EXTENSION_ID).exists())

    def test_missing_preferences_file_does_not_raise(self):
        """A profile Chrome has never finished writing still has to start."""
        self._install_files("0.2.1.6")

        processes._reset_stale_extension_profile(self.account)

        self.assertFalse((self.default / "Extensions" / EXTENSION_ID).exists())

    def test_unreadable_preferences_leaves_the_file_untouched(self):
        """Truncated/corrupt JSON is not something to overwrite blindly - the
        files still get cleared, and Chrome recovers Preferences itself."""
        self._install_files("0.2.1.6")
        (self.default / "Preferences").write_text("{not json", encoding="utf-8")

        processes._reset_stale_extension_profile(self.account)

        self.assertEqual(
            (self.default / "Preferences").read_text(encoding="utf-8"), "{not json",
        )

    def test_no_packed_extension_means_no_reset(self):
        """Without the marker files there is no target version to compare
        against, so an existing install must not be torn down."""
        installed = self._install_files("0.2.1.6")
        self._write_preferences("0.2.1.6")
        with mock.patch.object(processes, "_EXTENSION_VERSION_FILE", Path("/nonexistent")):
            processes._reset_stale_extension_profile(self.account)

        self.assertTrue(installed.is_dir())
        self.assertIn(EXTENSION_ID, self._settings())


class StartChromeResetGateTests(unittest.IsolatedAsyncioTestCase):
    """Who is allowed to have the profile rewritten under them.

    A supervisor restart forgets every process it started, so
    `ManagedProcess.running` reads False for a Chrome that is still very much
    alive - the orphan `start()` goes on to adopt. Resetting on that reading
    deleted a live Chrome's extension while `start()` adopted rather than
    relaunched it, so nothing reinstalled: stale extension still serving
    customers, stored extension state gone, and the record written back on
    exit with no files behind it. Confirmed live end to end.
    """

    def _account_processes(self, orphan_pid):
        chrome = SimpleNamespace(
            running=False,  # what a freshly restarted supervisor always sees
            command=["google-chrome", "--user-data-dir=/profiles/acct-1"],
            start=AsyncMock(),
        )
        procs = processes.AccountProcesses.__new__(processes.AccountProcesses)
        procs.account = SimpleNamespace(id="acct-1", profile_dir="/profiles/acct-1")
        procs.chrome = chrome
        # start_chrome() now also brings up this workspace's own display
        # stack first - not under test here, so each is a no-op stand-in.
        procs.xvfb = SimpleNamespace(start=AsyncMock())
        procs.x11vnc = SimpleNamespace(start=AsyncMock())
        procs.novnc = SimpleNamespace(start=AsyncMock())
        procs._ensure_xauth = lambda: None
        # start_chrome() recomputes the command before spawning (egw opt-in
        # may have changed); the fake account has no ports, so hand back the
        # command this test already declared.
        procs._chrome_command = lambda: chrome.command
        return procs

    async def _start_with_orphan(self, orphan_pid):
        procs = self._account_processes(orphan_pid)

        with (
            mock.patch.object(processes, "_repack_extension_if_needed", AsyncMock()),
            mock.patch.object(processes, "_find_running_pid", return_value=orphan_pid),
            mock.patch.object(processes, "_reset_stale_extension_profile") as reset,
            mock.patch.object(processes.asyncio, "sleep", AsyncMock()),
        ):
            await procs.start_chrome()
        return reset, procs.chrome.start

    async def test_a_live_orphan_chrome_is_never_reset_under(self):
        reset, start = await self._start_with_orphan(4242)

        reset.assert_not_called()
        start.assert_awaited_once()

    async def test_with_no_chrome_alive_the_reset_still_runs(self):
        """The gate must not disarm the feature itself: with nothing running,
        this is exactly the launch that has to get a clean profile."""
        reset, start = await self._start_with_orphan(None)

        reset.assert_called_once()
        start.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
