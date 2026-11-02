"""`POST /api/runs` - the one state-changing route that checked nothing.

It accepted a list of steps, each naming an arbitrary `account_id`, and
answered `200` with a plan id before any permission had been looked at. The
per-step check inside the background task does fire (the task inherits a copy
of the request context), but by then the caller has their 200 and the refusal
shows up only as a failed step - and that copied context is a snapshot, so a
long sequential plan keeps running as a user whose access was revoked minutes
earlier.

The other half is memory: `plans` and `_tasks` were never pruned, so any
logged-in user could allocate background tasks and completed-task objects in a
loop, for the life of the process.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import auth, orchestrator as orchestrator_module
from dashboard_api.orchestrator import Orchestrator, RunPlan, RunStep


# `email` is here because auth.public_user() projects it onto every request's
# user; a row without it is not a row the middleware can work with.
ALICE = {"id": 2, "email": "alice@x.test", "is_active": True, "is_admin": False}
ADMIN = {"id": 1, "email": "admin@x.test", "is_active": True, "is_admin": True}
ROWS = {1: ADMIN, 2: ALICE}
GRANTS = {1: [], 2: ["acct-a"]}


class _Account:
    def __init__(self, account_id):
        self.id = account_id
        self.control_port = 28900


class RunsAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.user_id = ALICE["id"]
        self.accounts = {"acct-a": _Account("acct-a"), "acct-b": _Account("acct-b")}
        main.orchestrator.plans.clear()
        main.orchestrator._tasks.clear()
        self.addCleanup(main.orchestrator.plans.clear)
        self.addCleanup(main.orchestrator._tasks.clear)

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
                side_effect=lambda uid: list(GRANTS.get(uid, [])),
            ),
            mock.patch.object(
                main.registry, "get", side_effect=self.accounts.get,
            ),
            mock.patch.object(main.registry, "list", return_value=[]),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            # The plan would otherwise reach out to a workspace control server.
            mock.patch.object(
                main, "_submit_for_plan", new=mock.AsyncMock(return_value="task-1"),
            ),
            mock.patch.object(
                main, "_task_status_for_plan",
                new=mock.AsyncMock(return_value={"status": "completed"}),
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self) -> TestClient:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE, "probe-token")
        return client

    def _run(self, client, steps, mode="parallel"):
        return client.post("/api/runs", json={"mode": mode, "steps": steps})

    # ------------------------------------------------------- authorization
    def test_someone_elses_workspace_is_refused_and_nothing_is_created(self):
        with self._client() as client:
            response = self._run(
                client, [{"account_id": "acct-b", "capability": "scan_inbox"}],
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(main.orchestrator.plans, {})

    def test_a_mixed_plan_is_refused_whole(self):
        """One allowed step must not smuggle a disallowed one in beside it."""
        with self._client() as client:
            response = self._run(client, [
                {"account_id": "acct-a", "capability": "scan_inbox"},
                {"account_id": "acct-b", "capability": "scan_inbox"},
            ])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(main.orchestrator.plans, {})

    def test_an_unknown_workspace_is_a_404(self):
        with self._client() as client:
            response = self._run(
                client, [{"account_id": "nope", "capability": "scan_inbox"}],
            )

        self.assertEqual(response.status_code, 404)

    def test_an_allowed_plan_runs_and_is_owned_by_its_submitter(self):
        with self._client() as client:
            response = self._run(
                client, [{"account_id": "acct-a", "capability": "scan_inbox"}],
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["owner_id"], ALICE["id"])

    # ------------------------------------------------------------ shape
    def test_a_plan_is_bounded_and_well_formed(self):
        with self._client() as client:
            too_many = self._run(client, [
                {"account_id": "acct-a", "capability": "scan_inbox"}
            ] * (main.MAX_RUN_STEPS + 1))
            self.assertEqual(too_many.status_code, 400)

            no_capability = self._run(client, [{"account_id": "acct-a"}])
            self.assertEqual(no_capability.status_code, 400)

            bad_payload = self._run(client, [
                {"account_id": "acct-a", "capability": "scan_inbox", "payload": "nope"},
            ])
            self.assertEqual(bad_payload.status_code, 400)

            self.assertEqual(self._run(client, []).status_code, 400)

    # -------------------------------------------------------- visibility
    def test_a_plan_is_visible_to_its_owner_and_to_admins_only(self):
        with self._client() as client:
            self._run(client, [{"account_id": "acct-a", "capability": "scan_inbox"}])
            self.assertEqual(len(client.get("/api/runs").json()), 1)

            # Someone else with the same workspace grant still does not own it.
            with mock.patch.dict(GRANTS, {3: ["acct-a"]}):
                ROWS[3] = {"id": 3, "email": "carol@x.test",
                           "is_active": True, "is_admin": False}
                self.addCleanup(ROWS.pop, 3, None)
                self.user_id = 3
                self.assertEqual(len(client.get("/api/runs").json()), 0)

            self.user_id = ADMIN["id"]
            self.assertEqual(len(client.get("/api/runs").json()), 1)


class PlanRetentionTests(unittest.TestCase):
    """The leak half, exercised directly on the orchestrator."""

    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(
            submit=mock.AsyncMock(return_value="t"),
            task_status=mock.AsyncMock(return_value={"status": "completed"}),
            emit=mock.Mock(),
        )

    @staticmethod
    def _finished(orchestrator, plan_id, minutes_ago, owner_id=1):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        plan = RunPlan(
            id=plan_id, mode="parallel",
            steps=[RunStep(account_id="acct-a", capability="scan_inbox")],
            status="completed", owner_id=owner_id,
            created_at=when.isoformat(), finished_at=when.isoformat(),
        )
        orchestrator.plans[plan_id] = plan
        orchestrator._tasks[plan_id] = mock.Mock(done=lambda: True)
        return plan

    def test_a_finished_plan_past_its_ttl_is_dropped_with_its_task(self):
        orchestrator = self._orchestrator()
        self._finished(orchestrator, "old", minutes_ago=60 * 24)
        self._finished(orchestrator, "recent", minutes_ago=1)

        orchestrator._prune()

        self.assertEqual(set(orchestrator.plans), {"recent"})
        # The task map is the heavier leak: a completed Task pins its result
        # and traceback.
        self.assertEqual(set(orchestrator._tasks), {"recent"})

    def test_the_oldest_finished_plans_go_when_there_are_too_many(self):
        orchestrator = self._orchestrator()
        for index in range(orchestrator_module.MAX_RETAINED_PLANS + 10):
            self._finished(orchestrator, f"plan-{index:04d}", minutes_ago=index)

        orchestrator._prune()

        self.assertEqual(
            len(orchestrator.plans), orchestrator_module.MAX_RETAINED_PLANS,
        )
        self.assertEqual(len(orchestrator._tasks), len(orchestrator.plans))
        # Oldest first: the most recent ones are the ones a dashboard reads.
        self.assertIn("plan-0000", orchestrator.plans)
        self.assertNotIn(f"plan-{orchestrator_module.MAX_RETAINED_PLANS + 9:04d}",
                         orchestrator.plans)

    def test_a_running_plan_is_never_pruned(self):
        orchestrator = self._orchestrator()
        running = RunPlan(
            id="live", mode="parallel",
            steps=[RunStep(account_id="acct-a", capability="scan_inbox")],
        )
        orchestrator.plans["live"] = running
        for index in range(orchestrator_module.MAX_RETAINED_PLANS + 5):
            self._finished(orchestrator, f"done-{index:04d}", minutes_ago=index)

        orchestrator._prune()

        self.assertIn("live", orchestrator.plans)

    def test_running_plans_are_counted_per_owner(self):
        orchestrator = self._orchestrator()
        orchestrator.plans["a"] = RunPlan(id="a", mode="parallel", steps=[], owner_id=7)
        orchestrator.plans["b"] = RunPlan(id="b", mode="parallel", steps=[], owner_id=7)
        self._finished(orchestrator, "c", minutes_ago=1, owner_id=7)

        self.assertEqual(orchestrator.running_plans_for(7), 2)
        self.assertEqual(orchestrator.running_plans_for(8), 0)
        self.assertEqual(orchestrator.running_plans_for(None), 0)


class FinishedEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_finish_event_names_the_accounts_it_ran_in(self):
        """It used to emit with an empty account_id, which the hub now routes
        to admins only - i.e. the people whose workspace it ran in would stop
        seeing their own run finish."""
        emitted = []
        orchestrator = Orchestrator(
            submit=mock.AsyncMock(return_value="t"),
            task_status=mock.AsyncMock(return_value={"status": "completed"}),
            emit=lambda account_id, event, **payload: emitted.append((account_id, event)),
        )
        plan = orchestrator.start("parallel", [
            {"account_id": "acct-a", "capability": "scan_inbox"},
            {"account_id": "acct-b", "capability": "scan_inbox"},
        ])
        await asyncio.wait_for(orchestrator._tasks[plan.id], timeout=5)

        finished = [account for account, event in emitted if event == "runplan.finished"]
        self.assertEqual(sorted(finished), ["acct-a", "acct-b"])
        self.assertNotIn("", finished)


if __name__ == "__main__":
    unittest.main()
