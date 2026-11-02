"""A workspace left asleep by a previous supervisor is woken at boot.

Sleep state lives in the supervisor's memory. A restart - or turning
`sleep.enabled` off - used to leave a sleeping workspace with its agent paused
and its Chrome stopped, and nothing that would ever wake it again.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api.sleep_wake import LifecycleState, SleepWakeManager


class FakeChrome:
    def __init__(self):
        self.running = False


class FakeManaged:
    def __init__(self):
        self.chrome = FakeChrome()
        self.started = 0

    async def start_chrome(self):
        self.started += 1
        self.chrome.running = True


class FakeConfig:
    def __init__(self, enabled):
        self.values = {"sleep.enabled": enabled}

    def get(self, key, default=None):
        return self.values.get(key, default)


class SleepRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, statuses, *, enabled=False):
        accounts = {account_id: SimpleNamespace(id=account_id, cdp_port=0, pinned=False)
                    for account_id in statuses}
        self.calls = []

        async def control(account, method, path, body=None):
            self.calls.append((account.id, method, path))
            status = statuses[account.id]
            if isinstance(status, Exception):
                raise status
            if path == "/control/resume":
                status["paused"] = False
                status["pause_reason"] = ""
            return status

        self.processes = {account_id: FakeManaged() for account_id in statuses}
        manager = SleepWakeManager(
            registry=SimpleNamespace(get=accounts.get),
            processes=self.processes,
            hub=SimpleNamespace(emit=lambda *args, **kwargs: None),
            control=control,
            config=FakeConfig(enabled),
        )

        async def cdp_ready(account):
            return True

        manager._wait_for_cdp = cdp_ready
        return manager

    async def test_an_auto_slept_workspace_is_woken(self):
        manager = self.make({"shop-a": {"paused": True, "pause_reason": "auto_sleep"}})

        self.assertEqual(await manager.recover_left_asleep(), ["shop-a"])
        self.assertEqual(self.processes["shop-a"].started, 1)
        self.assertIn(("shop-a", "POST", "/control/resume"), self.calls)
        self.assertEqual(manager.snapshot("shop-a")["lifecycle_state"],
                         LifecycleState.ACTIVE.value)

    async def test_an_operator_pause_is_left_alone(self):
        manager = self.make({"shop-a": {"paused": True, "pause_reason": "manual"}})
        self.assertEqual(await manager.recover_left_asleep(), [])
        self.assertEqual(self.processes["shop-a"].started, 0)

    async def test_running_and_unreachable_workspaces_are_left_alone(self):
        manager = self.make({
            "awake": {"paused": False, "pause_reason": ""},
            "down": ConnectionError("agent not running"),
        })
        self.assertEqual(await manager.recover_left_asleep(), [])
        self.assertEqual(sum(m.started for m in self.processes.values()), 0)

    async def test_recovery_runs_at_start_even_with_sleep_disabled(self):
        """Turning sleep off is exactly when a sleeping workspace would
        otherwise be stranded for good."""
        manager = self.make({"shop-a": {"paused": True, "pause_reason": "auto_sleep"}},
                            enabled=False)
        manager.start()
        for _ in range(20):
            await asyncio.sleep(0)
            if self.processes["shop-a"].started:
                break
        await manager.stop()
        self.assertEqual(self.processes["shop-a"].started, 1)
        self.assertIsNone(manager._sweep_task)


if __name__ == "__main__":
    unittest.main()
