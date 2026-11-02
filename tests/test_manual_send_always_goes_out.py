"""A message a person typed in the dashboard goes out even while the
workspace is paused or outside its operating hours.

A pause and closed hours stop the *agent's* own work (scans, replies,
follow-ups). They used to stop every task, so a person who had taken a
conversation over and replied by hand saw the message sit in the queue
until the workspace was resumed or the shop opened.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.graph import AgentGraph
from models.auth import AuthState
from models.tasks import Task
from storage.repositories import TaskRepository
from tests.support import make_database, unscoped_connection


def task(task_id, origin):
    return Task(account_id="acc_test", id=task_id, capability="send_message",
                platform="tiktok_web", payload={"origin": origin})


class RecordingScheduler:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    async def next_task(self, *, human_only=False):
        self.calls.append(human_only)
        for index, item in enumerate(self.queue):
            if not human_only or item.payload.get("origin") == "human":
                return self.queue.pop(index)
        return None


def graph_with(queue, *, paused=True, pause_reason="manual", open_hours=True):
    graph = AgentGraph.__new__(AgentGraph)
    graph.state = SimpleNamespace(
        running=True, paused=paused, pause_reason=pause_reason,
        auth_enforced=False, auth_state=AuthState.LOGGED_IN, current_task_id=None,
    )
    graph.idle_seconds = 0.01
    graph._task_submitted = asyncio.Event()
    graph.schedule = SimpleNamespace(is_open=lambda: open_hours, describe=lambda: "test")
    graph.scheduler = RecordingScheduler(queue)
    graph.ran = []

    async def ensure_graph():
        return None

    async def run_task(item):
        graph.ran.append(item.id)

    graph._ensure_graph = ensure_graph
    graph.run_task = run_task
    return graph


async def run_briefly(graph, seconds=0.1):
    loop = asyncio.create_task(graph.run())
    await asyncio.sleep(seconds)
    graph.state.running = False
    await asyncio.wait_for(loop, 1)


class PausedLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_paused_workspace_still_sends_what_a_person_typed(self):
        graph = graph_with([task("scan-1", "agent"), task("human-1", "human")])
        await run_briefly(graph)
        self.assertEqual(graph.ran, ["human-1"])
        # Only ever asked for human messages while paused.
        self.assertTrue(all(graph.scheduler.calls))

    async def test_outside_operating_hours_too(self):
        graph = graph_with([task("human-1", "human")], paused=False, open_hours=False)
        await run_briefly(graph)
        self.assertEqual(graph.ran, ["human-1"])

    async def test_an_asleep_workspace_claims_nothing(self):
        """Chrome is stopped while asleep; the dashboard heartbeat wakes it."""
        graph = graph_with([task("human-1", "human")], pause_reason="auto_sleep")
        await run_briefly(graph)
        self.assertEqual(graph.ran, [])
        self.assertEqual(graph.scheduler.calls, [])

    async def test_an_open_workspace_runs_everything_as_before(self):
        graph = graph_with([task("scan-1", "agent")], paused=False)
        await run_briefly(graph)
        self.assertEqual(graph.ran, ["scan-1"])


class HumanOnlyClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.repository = TaskRepository(self.database)
        admin = unscoped_connection(self, self.database)
        for task_id, capability, origin, priority in (
            ("scan-1", "messaging.scan_inbox", "agent", 5),
            ("human-1", "send_message", "human", 100),
        ):
            admin.execute(
                "INSERT INTO tasks(account_id, id, capability, platform, priority, payload, "
                "status) VALUES ('acc_test', %s, %s, 'tiktok_web', %s, %s, 'pending')",
                (task_id, capability, priority, json.dumps({"origin": origin})),
            )

    async def test_only_a_human_message_is_claimed(self):
        claimed = await self.repository.get_next_task(human_only=True)
        self.assertEqual(claimed.id, "human-1")
        self.assertIsNone(await self.repository.get_next_task(human_only=True))
        # The agent's own task is still there for when the workspace resumes.
        self.assertEqual((await self.repository.get_next_task()).id, "scan-1")


if __name__ == "__main__":
    unittest.main()
