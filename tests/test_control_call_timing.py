"""How long an operator's control call really took, in the log.

A takeover is two indexed queries, yet the whole call can still be slow: the
agent embeds its control server in its own event loop, so a stretch of the
agent that does not yield delays the request before the handler ever runs.
That wait was invisible - the only timing anyone could quote was a guess.

`control.server` now logs the handler's own `elapsed_ms` (database work), and
`_control` here logs the whole call. The difference between the two is the
wait, and a failed call reports how long it waited too: refused in a
millisecond means the workspace is stopped, while a timeout after
`CONTROL_TIMEOUT` means the agent is up but never got to the request.
"""

import sys
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from dashboard_api import main


def account(port=28999):
    return SimpleNamespace(id="acc_test", control_port=port)


class SlowClient:
    """An httpx client whose request takes `delay` seconds."""

    def __init__(self, delay, *args, **kwargs):
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, json=None, headers=None):
        import asyncio

        await asyncio.sleep(self.delay)
        # `raise_for_status` needs the request on the response, exactly as a
        # real transport sets it.
        return httpx.Response(
            200, json={"mode": "HUMAN"},
            request=httpx.Request(method, url),
        )


class ControlCallTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_call_is_logged_with_its_duration(self):
        delay = (main.CONTROL_SLOW_CALL_MS / 1000) + 0.05
        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kwargs: SlowClient(delay)
        ):
            with self.assertLogs("dashboard_api.main", level="INFO") as captured:
                result = await main._control(
                    account(), "POST", "/control/conversations/c-1/takeover",
                    {"operator_id": "operator-1"},
                )

        self.assertEqual(result, {"mode": "HUMAN"})
        line = next(item for item in captured.output if "slow call" in item)
        self.assertIn("/control/conversations/c-1/takeover", line)
        self.assertGreaterEqual(
            float(line.rsplit("elapsed_ms=", 1)[1]), main.CONTROL_SLOW_CALL_MS
        )

    async def test_a_fast_call_does_not_add_a_line_at_info(self):
        """Every status poll goes through here; only the slow ones are news."""
        with mock.patch.object(
            main.httpx, "AsyncClient", lambda **kwargs: SlowClient(0)
        ):
            with mock.patch.object(main.logger, "info") as info:
                await main._control(account(), "GET", "/control/status")

        self.assertEqual(info.call_args_list, [])

    async def test_a_stopped_workspace_reports_how_long_it_waited(self):
        """Nothing is listening on this port, so this is the real refusal
        path - the one an operator hits when the workspace is stopped."""
        with self.assertLogs("dashboard_api.main", level="INFO") as captured:
            with self.assertRaises(main.HTTPException) as raised:
                await main._control(
                    account(port=28999), "POST",
                    "/control/conversations/c-1/takeover", {"operator_id": "op"},
                )

        self.assertEqual(raised.exception.status_code, 503)
        line = next(item for item in captured.output if "call failed" in item)
        self.assertIn("error_type=ConnectError", line)
        # Refused, not waited on: this is what tells the two failures apart.
        self.assertLess(float(line.rsplit("elapsed_ms=", 1)[1].split()[0]), 1000.0)


if __name__ == "__main__":
    unittest.main()
