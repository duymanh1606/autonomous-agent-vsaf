"""The "Nhịp quét & trả lời" card: GET/PATCH /api/accounts/{id}/settings.

What used to be wrong: nothing could be changed while the agent was stopped,
the page never showed the current values, and the min/max were HTML
attributes only - any number reached the agent. The quiet period could also be
set at or above reply_wait_timeout_seconds, which silently pushes every
customer to a later scan.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.main as main
from load_config import load_config

INTERVAL = main.INTERVAL_KEY
QUIET = main.QUIET_PERIOD_KEY


class PacingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shop.yaml"
        base = yaml.safe_load(Path("configs/config.yaml").read_text(encoding="utf-8"))
        base["messaging"]["auto_check"].update({
            "interval_seconds": 20,
            "reply_quiet_period_seconds": 10,
            "reply_wait_timeout_seconds": 30,
        })
        self.path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")

        self.agent_running = False
        self.control = mock.AsyncMock(
            side_effect=lambda account, method, path, body: {
                "applied": body["values"], "restart_required": {}, "rejected": {},
            },
        )
        for target, value in (
            ("_account_or_404", lambda account_id: SimpleNamespace(id=account_id)),
            ("_control", self.control),
        ):
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("config", lambda account: load_config(self.path)),
        ):
            patcher = mock.patch.object(main.registry, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # `running` read live, so a test can flip self.agent_running.
        test = self

        class _Agent:
            @property
            def running(self):
                return test.agent_running

        patcher = mock.patch.dict(
            main.processes, {"shop": SimpleNamespace(agent=_Agent())}, clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch(self, values):
        return asyncio.run(main.patch_settings("shop", values))

    def _on_disk(self, key):
        return load_config(self.path).get(key)

    def test_reads_the_current_values_and_the_allowed_ranges(self):
        data = main.read_settings("shop")
        self.assertEqual(data["values"], {INTERVAL: 20, QUIET: 10})
        self.assertEqual(data["limits"][INTERVAL], {"min": 5, "max": 600})
        # Capped below reply_wait_timeout_seconds (30), not the flat 300.
        self.assertEqual(data["limits"][QUIET], {"min": 0, "max": 29})

    def test_a_stopped_agent_still_saves_to_the_config_file(self):
        result = self._patch({INTERVAL: 60, QUIET: 15})

        self.assertEqual(set(result["restart_required"]), {INTERVAL, QUIET})
        self.assertEqual(result["rejected"], {})
        self.assertEqual(self._on_disk(INTERVAL), 60)
        self.assertEqual(self._on_disk(QUIET), 15)
        self.control.assert_not_called()

    def test_a_running_agent_is_the_one_that_writes(self):
        self.agent_running = True
        result = self._patch({QUIET: 15})

        self.control.assert_awaited_once()
        self.assertEqual(result["applied"], {QUIET: 15})
        # Left to the agent, whose next save would overwrite a write from here.
        self.assertEqual(self._on_disk(QUIET), 10)

    def test_out_of_range_values_never_reach_the_agent_or_the_file(self):
        for running in (False, True):
            with self.subTest(agent_running=running):
                self.agent_running = running
                self.control.reset_mock()
                result = self._patch({INTERVAL: 1, QUIET: 30})
                self.assertEqual(set(result["rejected"]), {INTERVAL, QUIET})
                self.control.assert_not_called()
                self.assertEqual(self._on_disk(INTERVAL), 20)
                self.assertEqual(self._on_disk(QUIET), 10)

    def test_non_numbers_are_refused(self):
        for value in ("20", True, float("nan"), None):
            with self.subTest(value=value):
                result = self._patch({INTERVAL: value})
                self.assertIn(INTERVAL, result["rejected"])
        self.assertEqual(self._on_disk(INTERVAL), 20)

    def test_one_bad_value_does_not_block_the_good_one(self):
        self.agent_running = True
        result = self._patch({INTERVAL: 1, QUIET: 15})
        self.assertEqual(result["applied"], {QUIET: 15})
        self.assertIn(INTERVAL, result["rejected"])

    def test_other_keys_still_need_a_running_agent(self):
        result = self._patch({"products.lookup_enabled": False})
        self.assertIn("products.lookup_enabled", result["rejected"])
        self.control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
