"""Which workspaces the "customers waiting" alert should judge at all.

A workspace an operator paused or stopped leaves customers waiting on
purpose; paging about it is a false alarm. One that should be running but
does not answer is a real fault, and must not be hidden here.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import main


class ExpectedToAnswerTests(unittest.TestCase):
    def setUp(self):
        account = SimpleNamespace(id="shop-a", control_port=28999)
        patches = (
            mock.patch.object(main.registry, "get",
                              lambda account_id: account if account_id == "shop-a" else None),
            mock.patch.dict(main.processes, {"shop-a": object()}, clear=False),
            mock.patch.object(main.hub, "is_following", lambda account_id: True),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self, payload=None, error=None):
        def get(url, headers=None, timeout=None):
            if error is not None:
                raise error
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return mock.patch.object(main.httpx, "get", get)

    def test_a_running_agent_answers(self):
        with self.status({"paused": False}):
            self.assertTrue(main._expected_to_answer("shop-a"))

    def test_a_paused_agent_does_not(self):
        for reason in ("manual", "auto_sleep"):
            with self.subTest(reason=reason), self.status({"paused": True, "pause_reason": reason}):
                self.assertFalse(main._expected_to_answer("shop-a"))

    def test_a_workspace_an_operator_stopped_does_not(self):
        with mock.patch.object(main.hub, "is_following", lambda account_id: False):
            self.assertFalse(main._expected_to_answer("shop-a"))

    def test_an_agent_that_should_run_but_cannot_be_reached_still_counts(self):
        with self.status(error=httpx.ConnectError("refused")):
            self.assertTrue(main._expected_to_answer("shop-a"))

    def test_an_unknown_workspace_does_not(self):
        self.assertFalse(main._expected_to_answer("nope"))


if __name__ == "__main__":
    unittest.main()
