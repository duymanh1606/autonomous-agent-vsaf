"""Run one capability across several accounts, in parallel or one after another.

This lives entirely in the supervisor. No agent is aware that it is part of a
run plan, so nothing about multi-account orchestration can change how a single
agent behaves - which is what keeps the agents independently correct.

"Sequential" waits for each account's task to actually finish, read from that
account's own database, rather than assuming a fixed delay.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4


logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
DEFAULT_TASK_TIMEOUT = 600.0

# How many finished plans to keep for the dashboard to read back, and how long
# a finished one stays interesting. Without this, `plans` and `_tasks` grew for
# the life of the process - and a completed asyncio.Task keeps its result and
# traceback alive, so the task map was the heavier of the two leaks.
MAX_RETAINED_PLANS = 200
FINISHED_PLAN_TTL_SECONDS = 6 * 3600
# A ceiling on concurrent plans per owner. Anyone who can call POST /api/runs
# can otherwise allocate background tasks in a loop.
MAX_RUNNING_PLANS_PER_OWNER = 10

TERMINAL_STATUSES = {"completed", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    try:
        return (
            datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)
        ).total_seconds()
    except ValueError:
        return 0.0


@dataclass
class RunStep:
    account_id: str
    capability: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"      # pending | running | completed | failed | skipped
    task_id: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "capability": self.capability,
            "status": self.status, "task_id": self.task_id, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


@dataclass
class RunPlan:
    id: str
    mode: str                    # "parallel" | "sequential"
    steps: list[RunStep]
    status: str = "running"
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    # Who submitted it. Visibility used to be derived purely from the steps'
    # accounts, which is still the fallback for plans created before this
    # field existed, but the submitter is the honest answer.
    owner_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "mode": self.mode, "status": self.status,
            "owner_id": self.owner_id,
            "created_at": self.created_at, "finished_at": self.finished_at,
            "steps": [step.as_dict() for step in self.steps],
        }


class Orchestrator:
    def __init__(self, submit: Callable, task_status: Callable, emit: Callable):
        # submit(account_id, capability, payload) -> task_id
        # task_status(account_id, task_id) -> dict | None
        # emit(account_id, event_type, **payload) -> None
        self.submit = submit
        self.task_status = task_status
        self.emit = emit
        self.plans: dict[str, RunPlan] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def running_plans_for(self, owner_id: int | None) -> int:
        if owner_id is None:
            return 0
        return sum(
            1 for plan in self.plans.values()
            if plan.owner_id == owner_id and plan.finished_at is None
        )

    def _prune(self) -> None:
        """Forget finished plans that nobody is going to read again.

        `plans` and `_tasks` are pruned together on purpose: they are keyed
        alike, and dropping one without the other just moves the leak.
        """
        finished = [
            (plan.finished_at, plan.id) for plan in self.plans.values()
            if plan.finished_at is not None
        ]
        stale = {
            plan_id for finished_at, plan_id in finished
            if _age_seconds(finished_at) > FINISHED_PLAN_TTL_SECONDS
        }
        if len(self.plans) > MAX_RETAINED_PLANS:
            overflow = len(self.plans) - MAX_RETAINED_PLANS
            for _finished_at, plan_id in sorted(finished)[:overflow]:
                stale.add(plan_id)
        for plan_id in stale:
            self.plans.pop(plan_id, None)
            task = self._tasks.pop(plan_id, None)
            if task is not None and not task.done():
                # Only ever reachable if finished_at was set without the task
                # ending; cancelling is safer than dropping the reference.
                task.cancel()

    def start(self, mode: str, steps: list[dict[str, Any]],
              timeout: float = DEFAULT_TASK_TIMEOUT,
              owner_id: int | None = None) -> RunPlan:
        self._prune()
        plan = RunPlan(
            id=f"run-{uuid4().hex[:8]}",
            mode="sequential" if mode == "sequential" else "parallel",
            steps=[
                RunStep(
                    account_id=str(step["account_id"]),
                    capability=str(step["capability"]),
                    payload=dict(step.get("payload") or {}),
                )
                for step in steps
            ],
            owner_id=owner_id,
        )
        self.plans[plan.id] = plan
        self._tasks[plan.id] = asyncio.create_task(
            self._run(plan, timeout), name=f"runplan-{plan.id}"
        )
        return plan

    def cancel(self, plan_id: str) -> bool:
        task = self._tasks.get(plan_id)
        if task is None or task.done():
            return False
        task.cancel()
        plan = self.plans.get(plan_id)
        if plan is not None:
            plan.status = "cancelled"
            plan.finished_at = _now()
            # Only steps not yet dispatched are skipped: a task already sent to
            # an agent keeps running there, and saying otherwise would be a lie.
            for step in plan.steps:
                if step.status == "pending":
                    step.status = "skipped"
        return True

    # ---------------------------------------------------------- execution
    async def _run(self, plan: RunPlan, timeout: float) -> None:
        try:
            if plan.mode == "parallel":
                await asyncio.gather(
                    *(self._run_step(plan, step, timeout) for step in plan.steps),
                    return_exceptions=True,
                )
            else:
                for step in plan.steps:
                    await self._run_step(plan, step, timeout)
            plan.status = (
                "failed" if any(s.status == "failed" for s in plan.steps)
                else "completed"
            )
        except asyncio.CancelledError:
            plan.status = "cancelled"
            raise
        finally:
            plan.finished_at = _now()
            # One emit per account the plan touched, rather than a single
            # account-less event: the hub routes events to browsers by
            # account_id, and an empty one reaches admins only - which would
            # have silently hidden the end of a run from the very people whose
            # workspace it ran in.
            for account_id in dict.fromkeys(step.account_id for step in plan.steps):
                self.emit(account_id, "runplan.finished",
                          plan_id=plan.id, status=plan.status)

    async def _run_step(self, plan: RunPlan, step: RunStep, timeout: float) -> None:
        step.status = "running"
        step.started_at = _now()
        self.emit(step.account_id, "runplan.step", plan_id=plan.id,
                  capability=step.capability, status="running")
        try:
            step.task_id = await self.submit(
                step.account_id, step.capability, step.payload
            )
        except Exception as exc:
            logger.warning("[runs] step submit failed plan=%s", plan.id, exc_info=True)
            step.status = "failed"
            step.error = f"{type(exc).__name__}: {exc}"
            step.finished_at = _now()
            self.emit(step.account_id, "runplan.step", plan_id=plan.id,
                      capability=step.capability, status="failed")
            return

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            row = await self.task_status(step.account_id, step.task_id)
            if row and row.get("status") in TERMINAL_STATUSES:
                step.status = (
                    "completed" if row["status"] == "completed" else "failed"
                )
                step.error = row.get("error")
                step.finished_at = _now()
                self.emit(step.account_id, "runplan.step", plan_id=plan.id,
                          capability=step.capability, status=step.status)
                return
        # Timing out here does not stop the agent; the task may still finish.
        step.status = "failed"
        step.error = "timed out waiting for the task to finish"
        step.finished_at = _now()
        self.emit(step.account_id, "runplan.step", plan_id=plan.id,
                  capability=step.capability, status="failed")
