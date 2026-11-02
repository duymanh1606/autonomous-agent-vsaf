import asyncio
import inspect
import logging
from contextlib import suppress
from time import monotonic
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from core.checkpoint import AsyncAgentPostgresSaver
from core.policy import MESSAGING_CAPABILITIES
from models.account import AccountIsolationError, assert_account_scope, require_account_id
from models.action import Action
from models.auth import AuthState
from models.observation import Observation
from models.result import ActionResult
from models.tasks import Task
from models.ui_errors import TargetNotFound


logger = logging.getLogger(__name__)

FORBIDDEN_AUTH_CAPABILITIES = frozenset({
    "login", "log_in", "signin", "sign_in", "authenticate",
    "submit_login", "enter_credentials", "solve_captcha", "verify_account",
})


class WorkflowState(TypedDict, total=False):
    """Only serializable values are stored in durable checkpoints."""

    task: dict
    observation: dict | None
    result: dict | None
    runtime: dict


class AgentGraph:
    def __init__(
        self, scheduler, observer, router, planner, policy, recovery,
        tools, platform, memory, state, idle_seconds=3,
        auth_poll_seconds=5, auth_rate_limit_poll_seconds=60,
        temporary_backoff_seconds=(2, 5, 15),
        checkpoint_dsn=None, recursion_limit=1000,
        target_resolver=None, dataset_collector=None, action_verifier=None,
        account=None,
    ):
        # One worker serves exactly one account. The id is taken from the task
        # repository this loop writes through, so the account it *runs as* and
        # the account it *stores under* cannot drift apart.
        repository_account = getattr(getattr(memory, "repository", None), "account_id", None)
        self.account_id = require_account_id(
            getattr(account, "account_id", account) or repository_account
        ) if (account or repository_account) else None
        self.account = account
        self.scheduler = scheduler
        self.observer = observer
        self.router = router
        self.planner = planner
        self.policy = policy
        self.recovery = recovery
        self.tools = tools
        self.platform = platform
        self.memory = memory
        self.state = state
        self.idle_seconds = idle_seconds
        self.task = None
        self.observation = None
        self.auth_poll_seconds = auth_poll_seconds
        self.auth_rate_limit_poll_seconds = auth_rate_limit_poll_seconds
        self.temporary_backoff_seconds = tuple(temporary_backoff_seconds)
        # Checkpoints live in the shared PostgreSQL database now; the DSN
        # keeps every graph checkpoint in the shared PostgreSQL service.
        self.checkpoint_dsn = str(checkpoint_dsn) if checkpoint_dsn else None
        self.recursion_limit = int(recursion_limit)
        self.target_resolver = target_resolver
        self.dataset_collector = dataset_collector
        self.action_verifier = action_verifier
        self._checkpointer = InMemorySaver() if self.checkpoint_dsn is None else None
        self.graph = None
        # Attached after construction by main.py, once the control server's
        # event bus exists - exactly like HumanHandoff.events and
        # inbox_skill.events. Stays None for a runtime with no control
        # server (or none yet during boot); every publish below degrades to
        # a no-op rather than failing the auth-refresh it's reporting on.
        self.events = None
        # Which blocked auth state (if any) has already been announced, so
        # a workspace stuck on the same CAPTCHA/logout for many poll cycles
        # publishes exactly one notification instead of one every
        # auth_rate_limit_poll_seconds/auth_poll_seconds forever.
        self._last_notified_auth_state = None
        # Set by notify_task_submitted() so a task that arrives while the loop
        # is between polls is claimed immediately instead of up to
        # idle_seconds later. It only ends an idle wait early - claim order,
        # priority and every check inside run_once are untouched.
        self._task_submitted = asyncio.Event()

    async def _ensure_graph(self):
        if self.graph is not None:
            return self.graph
        if self.checkpoint_dsn is not None and self._checkpointer is None:
            self._checkpointer = await AsyncAgentPostgresSaver.from_dsn(
                self.checkpoint_dsn
            )
        builder = StateGraph(WorkflowState)
        builder.add_node("initialize", self._graph_initialize)
        builder.add_node("observe", self._graph_observe)
        builder.add_node("authenticate", self._graph_authenticate)
        builder.add_node("run_business_logic", self._graph_execute)
        builder.add_node("finalize", self._graph_finalize)
        builder.add_edge(START, "initialize")
        builder.add_conditional_edges(
            "initialize", self._after_initialize,
            {"observe": "observe", "finalize": "finalize"},
        )
        builder.add_edge("observe", "authenticate")
        builder.add_conditional_edges(
            "authenticate", self._after_authenticate,
            {"run_business_logic": "run_business_logic", "finalize": "finalize"},
        )
        builder.add_edge("run_business_logic", "finalize")
        builder.add_edge("finalize", END)
        self.graph = builder.compile(checkpointer=self._checkpointer)
        return self.graph

    async def close(self):
        if isinstance(self._checkpointer, AsyncAgentPostgresSaver):
            await self._checkpointer.aclose()
            self._checkpointer = None
            self.graph = None

    async def run(self):
        await self._ensure_graph()
        while self.state.running:
            if self.state.paused or not self._within_operating_hours():
                # A pause (or closed hours) stops the agent's own work: a task
                # already running finishes first, so it never stops halfway
                # through a send it cannot verify, and nothing new of its own
                # is claimed. A message a person typed in the dashboard is not
                # the agent's work and still goes out - except while asleep,
                # when Chrome is stopped and the dashboard's heartbeat is
                # already waking it.
                if self.state.pause_reason != "auto_sleep":
                    self._task_submitted.clear()
                    if await self.run_once(human_only=True):
                        continue
                await self._wait_for_task()
                continue
            # Cleared *before* claiming, so a task submitted at any point from
            # here on either gets claimed by this run_once or leaves the event
            # set and ends the wait below - there is no gap where a submit is
            # lost and the operator waits out the full idle tick.
            self._task_submitted.clear()
            ran_task = await self.run_once()
            if not ran_task:
                await self._wait_for_task()

    def notify_task_submitted(self):
        """Wake an idle loop now rather than at its next poll tick.

        Called by the control server's task submitter (main.py), which runs in
        this same event loop. A manual reply from the dashboard is the case
        that matters: the operator is watching the browser, and up to
        `idle_seconds` of the wait they saw was the loop sleeping on an empty
        queue that had just stopped being empty.
        """
        self._task_submitted.set()

    async def _wait_for_task(self):
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._task_submitted.wait(), self.idle_seconds)

    def _within_operating_hours(self):
        schedule = getattr(self, "schedule", None)
        if schedule is None or schedule.is_open():
            self._outside_hours_logged = False
            return True
        if not getattr(self, "_outside_hours_logged", False):
            logger.info(
                "Outside operating hours (%s); not claiming new tasks",
                schedule.describe(),
            )
            self._outside_hours_logged = True
        return False

    async def run_once(self, *, human_only=False):
        if self.state.auth_enforced and self.state.auth_state in {
            AuthState.AUTH_REQUIRED, AuthState.AUTH_RATE_LIMITED,
            AuthState.SECURITY_CHALLENGE, AuthState.TEMPORARY_ERROR,
        }:
            await self._refresh_blocked_auth()
            if self.state.auth_state != AuthState.LOGGED_IN:
                return False
        task = await (
            self.scheduler.next_task(human_only=True) if human_only
            else self.scheduler.next_task()
        )
        if task is None:
            return False
        await self.run_task(task)
        # Cleared here, not inside the LangGraph node graph itself: every node
        # (_graph_updates) reports current_task_id back into the checkpointed
        # state while the task is genuinely in flight, so clearing it mid-run
        # would corrupt a resumed checkpoint. Once run_task returns, the task
        # is unambiguously finished (success or not) - control/state.py's
        # /control/status reports this field as "is a task running right
        # now", and the dashboard's idle sweeper (dashboard_api/sleep_wake.py)
        # depends on it meaning exactly that to decide it is safe to sleep.
        self.state.current_task_id = None
        return True

    def _thread_id(self, task) -> str:
        return f"{self.account_id or 'unscoped'}:{task.id}"

    def _assert_account(self, task):
        """Task -> Runtime -> Browser must name one and the same account.

        This is the last cheap place to stop a mixed-up worker: past here the
        task opens a real browser profile and answers a real customer.
        """
        if self.account_id is None:
            return
        assert_account_scope(
            self.account_id, getattr(task, "account_id", None),
            boundary="Task -> Runtime",
        )
        browser_account = getattr(getattr(self, "tools", None), "account_id", None)
        browser_account = browser_account or getattr(self, "browser_account_id", None)
        if browser_account:
            assert_account_scope(
                self.account_id, browser_account, boundary="Runtime -> Browser",
            )

    async def run_task(self, task):
        try:
            self._assert_account(task)
        except AccountIsolationError as exc:
            logger.error(
                "Task rejected: account=%s id=%s reason=account_mismatch detail=%s",
                self.account_id, task.id, exc,
            )
            return ActionResult(success=False, error=str(exc))
        graph = await self._ensure_graph()
        config = {
            # Namespaced by account: two accounts share one database, and a
            # bare task id must never address another account's checkpoint.
            "configurable": {"thread_id": self._thread_id(task)},
            "recursion_limit": self.recursion_limit,
        }
        heartbeat = asyncio.create_task(self._renew_task_lease(task.id))
        try:
            snapshot = await graph.aget_state(config)
            if snapshot.values and snapshot.next:
                logger.info("Resuming task from LangGraph checkpoint: id=%s", task.id)
                output = await graph.ainvoke(None, config=config)
            else:
                output = await graph.ainvoke(
                    {"task": task.model_dump(mode="json"), "result": None, "runtime": {}},
                    config=config,
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        self._bind_graph_state(output)
        result = ActionResult.model_validate(output["result"])
        if task.capability == "messaging.scan_inbox":
            try:
                await self._checkpointer.adelete_thread(self._thread_id(task))
            except Exception as exc:
                logger.warning(
                    "Inbox scan checkpoint cleanup failed: task_id=%s error_type=%s",
                    task.id, type(exc).__name__, exc_info=True,
                )
        return result

    async def _renew_task_lease(self, task_id):
        """Keep a live claim fresh without holding a database transaction open."""
        repository = getattr(getattr(self, "memory", None), "repository", None)
        renew = getattr(repository, "renew_task_lease", None)
        if renew is None:
            return
        lease_seconds = max(10, int(getattr(repository, "task_lease_seconds", 180)))
        interval = max(3.0, lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await renew(task_id, lease_seconds)
            except Exception:
                logger.exception("Task lease renewal failed: task_id=%s", task_id)
                return
            if not renewed:
                logger.error("Task lease was lost: task_id=%s", task_id)
                return

    async def _graph_initialize(self, graph_state):
        self._bind_graph_state(graph_state)
        task = self.task
        self.state.begin_task(task.id)
        begin_observer_task = getattr(self.observer, "begin_task", None)
        if begin_observer_task:
            started = begin_observer_task()
            if inspect.isawaitable(started):
                await started
        platform_name = getattr(self.platform, "name", task.platform)
        logger.info(
            "Task started: id=%s capability=%s platform=%s",
            task.id, task.capability, task.platform,
        )
        if task.platform != platform_name:
            result = ActionResult(
                success=False,
                error=f"Task platform {task.platform!r} does not match active platform {platform_name!r}",
            )
            logger.error("Task rejected: id=%s reason=platform_mismatch", task.id)
            return self._graph_updates(result)
        if task.capability.casefold() in FORBIDDEN_AUTH_CAPABILITIES:
            result = ActionResult(
                success=False,
                error="AUTH_REQUIRED: automated login and verification tasks are prohibited",
                data={"auth_state": self.state.auth_state.value},
            )
            logger.warning("Task rejected: id=%s reason=forbidden_auth_capability", task.id)
            return self._graph_updates(result)
        return self._graph_updates()

    @staticmethod
    async def _after_initialize(graph_state):
        return "finalize" if graph_state.get("result") else "observe"

    async def _graph_observe(self, graph_state):
        self._bind_graph_state(graph_state)
        self.observation = await self._observe()
        self.state.update_observation(self.observation)
        self._log_observation("initial", self.observation)
        return self._graph_updates()

    async def _graph_authenticate(self, graph_state):
        self._bind_graph_state(graph_state)
        task = self.task
        logger.debug("LangGraph node started: authenticate task_id=%s", task.id)
        auth_failure = await self._ensure_authenticated()
        if auth_failure:
            logger.warning("Task paused: id=%s reason=authentication", task.id)
            return self._graph_updates(auth_failure)

        if self.observation.security_challenge:
            result = ActionResult(success=False, error="AUTH_RATE_LIMITED: manual security verification required")
            logger.warning("Task aborted: id=%s reason=security_challenge", task.id)
            return self._graph_updates(result)
        updates = self._graph_updates()
        logger.debug("LangGraph node finished: authenticate task_id=%s", task.id)
        return updates

    @staticmethod
    async def _after_authenticate(graph_state):
        logger.debug("LangGraph routing after authenticate")
        return "finalize" if graph_state.get("result") else "run_business_logic"

    async def _graph_execute(self, graph_state):
        self._bind_graph_state(graph_state)
        task = self.task
        try:
            logger.info(
                "Agent reasoning: task_id=%s stage=route_selection screen=%s auth=%s",
                task.id, self.observation.screen, self.observation.auth_state.value,
            )
            skill = await self.router.route(task)
            if skill:
                logger.info("Task route selected: id=%s route=skill", task.id)
                result = await skill.execute(task, self)
            else:
                logger.info("Task route selected: id=%s route=planner", task.id)
                result = await self._run_planner(task)
        except Exception as exc:
            result = ActionResult(success=False, error=str(exc))
            logger.exception(
                "Task execution failed: id=%s error_type=%s",
                task.id, type(exc).__name__,
            )
            # Selector/tool failures trigger a fresh DOM observation with a
            # vision fallback request. The vision layer remains read-only.
            try:
                self.observation = await self._observe(force_vision=True)
                self.state.update_observation(self.observation)
            except Exception:
                # The action already failed; a failed re-observation keeps
                # the previous observation, it must not mask that failure.
                logger.warning("Re-observation after a failed action failed",
                               exc_info=True)

        return self._graph_updates(result)

    async def _graph_finalize(self, graph_state):
        self._bind_graph_state(graph_state)
        result = ActionResult.model_validate(graph_state["result"])
        await self.memory.record_task(self.task, result)
        logger.info(
            "Task finished: id=%s success=%s actions=%s",
            self.task.id, result.success, self.state.action_count,
        )
        if result.success and self.task.capability in MESSAGING_CAPABILITIES:
            # A routine messaging.scan_inbox finding nothing must not look
            # like activity - only real customer-facing messaging work
            # (send/reply) resets the dashboard supervisor's idle timer for
            # the sleep/wake lifecycle.
            self.state.mark_business_activity()
        if (
            self.task.capability in {"send_message", "reply_to_message"}
            and str(self.task.payload.get("origin") or "") == "human"
        ):
            # Mirrors the auto-reply path (inbox_watcher._announce_message),
            # which fires only after its own DB write - this is the actual
            # completion of the `conversation_messages` INSERT that
            # TaskRepository.complete() does for a human-origin send (see
            # storage/repositories.py), unlike the synthetic same-named event
            # dashboard_api/main.py fires immediately on submit, before the
            # send is confirmed or the row exists. Without this, a manual
            # reply from the dashboard only ever appeared on the next poll.
            #
            # The failure side matters just as much: nothing is written to
            # `conversation_messages` for a send that was never verified, so
            # without an event the operator's optimistic bubble simply
            # vanishes on the next refetch and the message looks delivered.
            # `client_message_id` is what the dashboard generated for this
            # send, so it can mark that exact bubble instead of guessing.
            # `uncertain` separates "never went out" from "Send was pressed
            # but the thread did not prove it" - telling the operator the
            # second one did not reach the customer invites a duplicate.
            self._notify_human_send(
                "conversation.message.sent" if result.success
                else "conversation.message.failed",
                conversation_id=self.task.payload.get("conversation_id"),
                client_message_id=self.task.payload.get("client_message_id"),
                error="" if result.success else str(result.error or "send not verified"),
                uncertain=(
                    not result.success
                    and (result.data or {}).get("send_status") == "UNCERTAIN"
                ),
            )
        elif result.success and self.task.payload.get("automatic") is True:
            # A follow-up nudge's row is written by the same
            # TaskRepository.complete() call; no operator bubble waits on it,
            # so only the success needs announcing.
            self._notify_human_send(
                "conversation.message.sent",
                conversation_id=self.task.payload.get("conversation_id"),
                client_message_id=self.task.payload.get("client_message_id"),
                error="", origin="agent",
            )
        return self._graph_updates(result)

    def _notify_human_send(
        self, event_name, *, conversation_id, client_message_id, error, uncertain=False,
        origin="human",
    ):
        """Best-effort event publish - never breaks task completion."""
        publish = getattr(self.events, "publish", None)
        if publish is None or not conversation_id:
            return
        try:
            publish(
                event_name,
                account_id=self.account_id, conversation_id=conversation_id,
                client_message_id=client_message_id, origin=origin, error=error,
                uncertain=bool(uncertain),
            )
        except Exception:
            logger.exception("[graph] publishing %s failed", event_name)

    def _graph_updates(self, result=None):
        updates = {
            "task": self.task.model_dump(mode="json"),
            "observation": (
                self.observation.model_dump(mode="json") if self.observation else None
            ),
            "runtime": {
                "current_task_id": self.state.current_task_id,
                "retry_count": self.state.retry_count,
                "action_count": self.state.action_count,
                "task_runtime_seconds": self.state.task_runtime_seconds,
                "last_action": self.state.last_action,
                "auth_state": self.state.auth_state.value,
                "auth_enforced": self.state.auth_enforced,
                "auth_probe_attempted": self.state.auth_probe_attempted,
                "temporary_error_count": self.state.temporary_error_count,
                "metadata": self.state.metadata,
            },
        }
        if result is not None:
            updates["result"] = result.model_dump(mode="json")
        return updates

    def _bind_graph_state(self, graph_state):
        if graph_state.get("task"):
            self.task = Task.model_validate(graph_state["task"])
        runtime = graph_state.get("runtime") or {}
        if runtime and self.state.current_task_id != self.task.id:
            self.state.current_task_id = runtime.get("current_task_id")
            self.state.retry_count = int(runtime.get("retry_count", 0))
            self.state.action_count = int(runtime.get("action_count", 0))
            self.state.task_started_at = monotonic() - float(
                runtime.get("task_runtime_seconds", 0.0)
            )
            self.state.last_action = runtime.get("last_action")
            self.state.auth_state = AuthState(
                runtime.get("auth_state", AuthState.UNKNOWN.value)
            )
            self.state.auth_enforced = bool(
                runtime.get("auth_enforced", self.state.auth_enforced)
            )
            self.state.auth_probe_attempted = bool(
                runtime.get("auth_probe_attempted", False)
            )
            self.state.temporary_error_count = int(
                runtime.get("temporary_error_count", 0)
            )
            self.state.metadata = dict(runtime.get("metadata") or {})
        if graph_state.get("observation"):
            self.observation = Observation.model_validate(graph_state["observation"])
            self.state.update_observation(self.observation)

    async def _ensure_authenticated(self):
        if not self.state.auth_enforced:
            return None

        for attempt in range(len(self.temporary_backoff_seconds) + 1):
            auth_state = self.observation.auth_state
            if auth_state == AuthState.LOGGED_IN:
                self.state.confirm_logged_in()
                return None
            if auth_state == AuthState.AUTH_RATE_LIMITED:
                self.state.block_auth(self.auth_rate_limit_poll_seconds)
                return self._auth_result(
                    "AUTH_RATE_LIMITED: stop automation and resolve CAPTCHA/security verification manually"
                )
            if auth_state == AuthState.SECURITY_CHALLENGE:
                self.state.block_auth(self.auth_rate_limit_poll_seconds)
                return self._auth_result(
                    "SECURITY_CHALLENGE: stop automation and resolve verification manually"
                )
            if auth_state == AuthState.AUTH_REQUIRED:
                if await self._try_auto_login():
                    return None
                self.state.block_auth(self.auth_poll_seconds)
                return self._auth_result(
                    "AUTH_REQUIRED: log in manually in the agent Chrome window"
                )
            if auth_state == AuthState.TEMPORARY_ERROR:
                if attempt >= len(self.temporary_backoff_seconds):
                    self.state.block_auth(self.temporary_backoff_seconds[-1])
                    return self._auth_result("TEMPORARY_ERROR: authentication check cooldown active")
                await asyncio.sleep(self.temporary_backoff_seconds[attempt])
                self.observation = await self._observe()
                self.state.update_observation(self.observation)
                continue

            # UNKNOWN is validated at most once per blocked-auth cycle through
            # a protected route. It never submits an authentication form.
            if self.state.auth_probe_attempted:
                self.observation.auth_state = AuthState.AUTH_REQUIRED
                self.state.auth_state = AuthState.AUTH_REQUIRED
                self.state.block_auth(self.auth_poll_seconds)
                return self._auth_result(
                    "AUTH_REQUIRED: session is not confirmed; waiting for manual login"
                )
            self.state.auth_probe_attempted = True
            await self.execute_action(Action(
                name="browser.check_auth",
                reason="Validate the persisted Chrome session through a protected route",
            ))
            if self.observation.auth_state == AuthState.LOGGED_IN:
                self.state.confirm_logged_in()
                return None
            # Re-enter once to classify the result. UNKNOWN cannot trigger a
            # second protected-route navigation because auth_probe_attempted is set.
            continue

        return self._auth_result("AUTH_REQUIRED: authentication could not be confirmed")

    async def _refresh_blocked_auth(self):
        if self.state.auth_cooldown_active:
            return
        self.observation = await self._observe()
        self.state.update_observation(self.observation)
        if self.state.auth_state == AuthState.LOGGED_IN:
            was_blocked = self._last_notified_auth_state is not None
            self.state.confirm_logged_in()
            logger.info("Authentication confirmed: LOGGED_IN; resuming agent loop")
            if was_blocked:
                self._notify_auth("auth.recovered")
            self._last_notified_auth_state = None
        elif self.state.auth_state == AuthState.AUTH_RATE_LIMITED:
            self._notify_auth_once(AuthState.AUTH_RATE_LIMITED, "auth.rate_limited")
            self.state.block_auth(self.auth_rate_limit_poll_seconds)
        elif self.state.auth_state == AuthState.SECURITY_CHALLENGE:
            self._notify_auth_once(AuthState.SECURITY_CHALLENGE, "auth.security_challenge")
            self.state.block_auth(self.auth_rate_limit_poll_seconds)
        elif self.state.auth_state == AuthState.AUTH_REQUIRED:
            if not await self._try_auto_login():
                self._notify_auth_once(AuthState.AUTH_REQUIRED, "auth.required")
                self.state.block_auth(self.auth_poll_seconds)
        else:
            delay = self.temporary_backoff_seconds[
                min(self.state.temporary_error_count, len(self.temporary_backoff_seconds) - 1)
            ]
            self.state.temporary_error_count += 1
            self.state.block_auth(delay)

    def _notify_auth(self, event_name: str, **payload):
        """Best-effort event publish - a reporting failure must never break
        the auth-refresh loop it is reporting on (same posture as
        HumanHandoff._announce)."""
        publish = getattr(self.events, "publish", None)
        if publish is None:
            return
        try:
            publish(event_name, account_id=self.account_id, **payload)
        except Exception:
            logger.exception("[auth] publishing %s failed", event_name)

    def _notify_auth_once(self, auth_state: AuthState, event_name: str):
        """Announce a blocked auth state exactly once per transition into it.

        Without this, every poll while stuck on the same CAPTCHA/logout
        (every auth_rate_limit_poll_seconds/auth_poll_seconds) would publish
        a fresh event forever - real-time on first detection, not a spam
        timer.
        """
        if self._last_notified_auth_state == auth_state:
            return
        self._last_notified_auth_state = auth_state
        self._notify_auth(event_name, auth_state=auth_state.value)

    async def _try_auto_login(self) -> bool:
        """Use the trusted browser hook without exposing secrets to planning."""
        if not self.tools.exists("browser.auto_login"):
            return False
        result = await self.tools.execute(Action(name="browser.auto_login"))
        if result.get("auth_state") != AuthState.LOGGED_IN.value:
            return False
        self.observation = await self._observe()
        self.state.update_observation(self.observation)
        if self.observation.auth_state == AuthState.LOGGED_IN:
            self.state.confirm_logged_in()
            logger.info("Authentication confirmed after bounded auto-login")
            return True
        return False

    def _auth_result(self, error):
        logger.warning(error)
        return ActionResult(
            success=False,
            error=error,
            data={"auth_state": self.observation.auth_state.value},
        )

    async def _run_planner(self, task):
        while True:
            if self.platform.task_complete(task, self.observation):
                logger.info("Task completion verified: id=%s", task.id)
                return ActionResult(success=True, data={"screen": self.observation.screen})
            action = await self.planner.plan(task, self.observation, self.memory)
            logger.info(
                "Agent decision: task_id=%s action=%s confidence=%.2f",
                task.id, action.name, action.confidence,
            )
            result = await self.execute_action(action)
            if action.name == "abort_task":
                return result
            if not result.success:
                recovery_action = await self.recovery.recover(
                    task, action, self.observation, self.state
                )
                logger.warning(
                    "Recovery decision: task_id=%s failed_action=%s recovery_action=%s",
                    task.id, action.name, recovery_action.name,
                )
                recovery_result = await self.execute_action(recovery_action)
                if recovery_action.name == "abort_task" or not recovery_result.success:
                    return recovery_result

    async def execute_action(self, action):
        before_observation = self.observation
        before = before_observation.screen if before_observation else None
        decide = getattr(self.policy, "decide", None)
        policy_decision = (
            await decide(action, self.state, self.task)
            if decide is not None else None
        )
        allowed = (
            policy_decision.allowed if policy_decision is not None
            else await self.policy.allow(action, self.state, self.task)
        )
        if not allowed:
            logger.warning(
                "Policy rejected action: task_id=%s account_id=%s action=%s "
                "capability=%s risk=%s reason=%s",
                getattr(self.task, "id", "none"),
                getattr(policy_decision, "account_id", ""), action.name,
                getattr(policy_decision, "capability", ""),
                getattr(policy_decision, "risk_level", "unknown"),
                getattr(policy_decision, "reason", "rejected"),
            )
            return ActionResult(
                success=False,
                error=(
                    f"Policy rejected action: {action.name}: "
                    f"{getattr(policy_decision, 'reason', 'rejected')}"
                ),
                data={
                    "policy_decision": (
                        policy_decision.__dict__ if policy_decision is not None else {}
                    )
                },
            )
        logger.info(
            "Policy approved action: task_id=%s account_id=%s action=%s "
            "capability=%s risk=%s reason=%s",
            getattr(self.task, "id", "none"),
            getattr(policy_decision, "account_id", ""), action.name,
            getattr(policy_decision, "capability", ""),
            getattr(policy_decision, "risk_level", "unknown"),
            getattr(policy_decision, "reason", "approved"),
        )
        if action.name == "abort_task":
            return ActionResult(success=False, error=action.args.get("reason", "Task aborted"))

        if not await self.policy.reserve_send(action, self.task):
            logger.warning(
                "Send reservation rejected: task_id=%s action=%s",
                getattr(self.task, "id", "none"), action.name,
            )
            return ActionResult(
                success=False,
                error="Policy rejected duplicate or unverified message send attempt",
            )
        if action.name in {"platform.send_message", "platform.retry_send"}:
            # Count the actual Enter attempt, even if delivery verification is
            # later NOT_SENT or UNCERTAIN.
            self.policy.record(action, self.task)

        try:
            logger.info(
                "Tool execution started: task_id=%s action=%s",
                getattr(self.task, "id", "none"), action.name,
            )
            raw_result = await self.tools.execute(action)
            self.state.action_count += 1
            self.state.last_action = action.name
            # A tool exit code is never treated as UI verification.
            await self._observe_after_action(
                action, raw_result, "after_action", allow_vision=False
            )
            if action.name in {"platform.send_message", "platform.retry_send"}:
                await self.memory.repository.update_send_verification(
                    self.task.id,
                    action.args["message_signature"],
                    self.observation.message_send_state.value,
                    self.observation.perception_source,
                )
            verified = self._verify_action(
                action, before_observation, self.observation
            )
            if not verified and action.name in {
                "platform.open_conversation", "browser.input_text",
                "platform.send_message", "platform.retry_send",
                "platform.clear_message_draft",
            }:
                logger.info(
                    "DOM verification inconclusive: task_id=%s action=%s; requesting Vision fallback",
                    self.task.id, action.name,
                )
                expected = (
                    action.args.get("target")
                    if action.name == "platform.open_conversation"
                    else action.args.get("text")
                )
                self.observation = await self._observe(
                    force_vision=True,
                    after_action=action.name,
                    expected_message=expected,
                )
                self.state.update_observation(self.observation)
                self._log_observation("vision_verification", self.observation, action.name)
                verified = self._verify_action(
                    action, before_observation, self.observation
                )
                if action.name in {"platform.send_message", "platform.retry_send"}:
                    await self.memory.repository.update_send_verification(
                        self.task.id,
                        action.args["message_signature"],
                        self.observation.message_send_state.value,
                        self.observation.perception_source,
                    )
            tool_ok = raw_result.get("success", True) if isinstance(raw_result, dict) else True
            result = ActionResult(
                success=bool(tool_ok and verified),
                data={"tool": raw_result, "screen": self.observation.screen},
                error=None if tool_ok and verified else "UI state did not verify the action",
            )
            logger.info(
                "Action finished: task_id=%s action=%s tool_ok=%s verified=%s success=%s",
                self.task.id, action.name, bool(tool_ok), bool(verified), result.success,
            )
            await self._record_gui_action(
                action, raw_result, before_observation, bool(tool_ok and verified)
            )
            if result.success and action.name not in {
                "browser.snapshot", "browser.screenshot", "browser.back",
                "platform.send_message", "platform.retry_send",
            }:
                self.state.retry_count = 0
                self.policy.record(action, self.task)
        except TargetNotFound as exc:
            if action.name in {
                "platform.attach_message_media",
                "platform.send_message", "platform.retry_send",
            }:
                # Never replace a recipient-gated messaging mutation with a
                # coordinate or Vision click. That fallback would execute
                # outside the adapter's fresh active-chat check.
                result = ActionResult(success=False, error=str(exc))
                logger.warning(
                    "Messaging target missing: task_id=%s action=%s; "
                    "grounding_disabled_for_recipient_safety",
                    getattr(self.task, "id", "none"), action.name,
                )
                await self.memory.record_action(
                    self.task.id, action, result, before,
                    self.observation.screen if self.observation else None,
                )
                return result
            logger.info(
                "DOM target missing: task_id=%s action=%s target=%s; "
                "trying fixed coordinate before Gemini",
                getattr(self.task, "id", "none"), action.name, exc.target,
            )
            result = await self._ground_missing_target(
                action, exc, before_observation
            )
            logger.info(
                "Grounded action finished: task_id=%s action=%s success=%s",
                self.task.id, action.name, result.success,
            )
            if result.success and action.name not in {
                "browser.snapshot", "browser.screenshot", "browser.back",
                "platform.send_message", "platform.retry_send",
            }:
                self.state.retry_count = 0
                self.policy.record(action, self.task)
        except Exception as exc:
            result = ActionResult(success=False, error=str(exc))
            logger.exception(
                "Action failed: task_id=%s action=%s error_type=%s",
                getattr(self.task, "id", "none"), action.name, type(exc).__name__,
            )
            try:
                self.observation = await self._observe(force_vision=True)
                self.state.update_observation(self.observation)
            except Exception:
                # The action already failed; a failed re-observation keeps
                # the previous observation, it must not mask that failure.
                logger.warning("Re-observation after a failed action failed",
                               exc_info=True)

        await self.memory.record_action(
            self.task.id, action, result, before, self.observation.screen if self.observation else None
        )
        return result

    async def _ground_missing_target(self, action, error, before_observation):
        if self.target_resolver is None:
            return ActionResult(success=False, error=str(error))
        route = self.target_resolver.fixed.route_for_action(action.name)
        target = str(route.get("target") or error.target).strip().casefold()
        expected_state = route.get("expected_state")
        instruction = route.get("instruction") or action.reason or f"Click {target}"
        browser = getattr(self.platform, "browser", None)
        fixed_failed = None

        # DOM lookup may have taken time. Refresh state without Vision before
        # trusting a state-bound coordinate.
        self.observation = await self._observe(allow_vision=False)
        self.state.update_observation(self.observation)
        self._log_observation("before_fixed_coordinate", self.observation, action.name)
        fallback_observation = self.observation

        fixed = await self.target_resolver.resolve(
            state=fallback_observation.screen,
            target=target,
            observation=fallback_observation,
            allowed_sources=("fixed_coordinate",),
            expected_state=expected_state,
        )
        if fixed is not None and browser and hasattr(browser, "click_coordinate"):
            logger.info(
                "UI action: action=click target=%s source=fixed_coordinate "
                "state=%s coordinate=(%s,%s)",
                target,
                self.target_resolver.fixed.normalize_state(fallback_observation.screen),
                fixed.x, fixed.y,
            )
            try:
                raw = await browser.click_coordinate(
                    fixed.x, fixed.y,
                    target=target,
                    expected_screen=fixed.expected_state,
                    observed_viewport=fixed.viewport,
                )
                self.state.action_count += 1
                self.state.last_action = action.name
                await self._observe_after_action(
                    action, raw, "after_fixed_coordinate", allow_vision=False
                )
                verified = self._verify_action(
                    action, before_observation, self.observation
                )
                fixed_failed = (fixed, raw, verified)
                if verified:
                    await self._record_grounding_sample(
                        action, instruction, fixed, raw, fallback_observation,
                        True, expected_state, screenshot_path=None,
                    )
                    return ActionResult(
                        success=True,
                        data={"tool": raw, "screen": self.observation.screen},
                    )
                logger.warning(
                    "Fixed coordinate failed verification: action=%s target=%s "
                    "next_state=%s; escalating_to=gemini",
                    action.name, target, self.observation.screen,
                )
            except (RuntimeError, ValueError) as fixed_error:
                logger.warning(
                    "Fixed coordinate unavailable at click time: action=%s "
                    "target=%s reason=%s; escalating_to=gemini",
                    action.name, target, str(fixed_error),
                )
                fixed_failed = (fixed, {}, False)

        gemini_target = str(route.get("gemini_target") or error.target).strip()
        logger.info(
            "Gemini grounding requested: action=%s target=%s",
            action.name, gemini_target,
        )
        gemini = await self.target_resolver.resolve(
            state=self.observation.screen,
            target=target,
            observation=self.observation,
            allowed_sources=("gemini",),
            expected_state=expected_state,
            gemini_target=gemini_target,
        )
        grounded_observation = self.target_resolver.gemini.last_observation
        if grounded_observation is not None:
            self.observation = grounded_observation
            self.state.update_observation(self.observation)
            self._log_observation("gemini_grounding", self.observation, action.name)

        if fixed_failed is not None:
            fixed, raw, _ = fixed_failed
            await self._record_grounding_sample(
                action, instruction, fixed, raw, fallback_observation,
                False, expected_state,
                screenshot_path=getattr(grounded_observation, "screenshot_path", None),
            )

        if gemini is None or browser is None:
            await self._record_grounding_sample(
                action, instruction, None, {}, fallback_observation,
                False, expected_state,
                source="gemini",
                screenshot_path=getattr(grounded_observation, "screenshot_path", None),
            )
            return ActionResult(
                success=False,
                error=str(error),
                data={"error": error.as_dict(), "fallback": "gemini_grounding"},
            )

        logger.info(
            "UI action: action=click target=%s source=gemini state=%s "
            "coordinate=(%s,%s) confidence=%.2f",
            target,
            self.target_resolver.fixed.normalize_state(fallback_observation.screen),
            gemini.x, gemini.y, gemini.confidence,
        )
        try:
            raw = await browser.click_bbox(gemini.target_id)
            self.state.action_count += 1
            self.state.last_action = action.name
            await self._observe_after_action(
                action, raw, "after_gemini_click", allow_vision=False
            )
            verified = self._verify_action(
                action, before_observation, self.observation
            )
        except (RuntimeError, ValueError) as click_error:
            raw = {"success": False, "source": "gemini", "error": str(click_error)}
            verified = False
        await self._record_grounding_sample(
            action, instruction, gemini, raw, fallback_observation,
            verified, expected_state,
            screenshot_path=getattr(grounded_observation, "screenshot_path", None),
        )
        return ActionResult(
            success=bool(verified),
            data={
                "tool": raw,
                "screen": self.observation.screen,
                "recovered_from": error.as_dict(),
            },
            error=None if verified else "Gemini-grounded click did not verify the action",
        )

    async def _record_gui_action(
        self, action, raw_result, before_observation, success
    ):
        if self.dataset_collector is None or not isinstance(raw_result, dict):
            return
        source = raw_result.get("source")
        if source not in {"dom", "fixed_coordinate", "gemini"}:
            return
        route = (
            self.target_resolver.fixed.route_for_action(action.name)
            if self.target_resolver else {}
        )
        target = (
            route.get("target") or raw_result.get("target")
            or raw_result.get("target_type") or action.name
        )
        point = raw_result.get("clicked_point")
        logger.info(
            "UI action: action=click target=%s source=%s state=%s coordinate=%s",
            target,
            source,
            self.target_resolver.fixed.normalize_state(before_observation.screen)
            if self.target_resolver else before_observation.screen,
            tuple(point) if point else None,
        )
        viewport = (before_observation.metadata or {}).get("viewport") or {}
        await self._safe_collect_gui_sample(
            screen_state=before_observation.screen,
            target=target,
            instruction=route.get("instruction") or action.reason or action.name,
            source=source,
            predicted_point=raw_result.get("predicted_point") or point,
            clicked_point=point,
            viewport=(viewport.get("width"), viewport.get("height")),
            confidence=raw_result.get("confidence", 1.0 if source == "dom" else None),
            success=success,
            expected_state=(
                route.get("expected_state") or raw_result.get("expected_screen")
            ),
            next_state=self.observation.screen if self.observation else None,
            screenshot_path=before_observation.screenshot_path,
            observation=before_observation,
        )

    def _verify_action(self, action, before_observation, after_observation):
        if self.action_verifier is not None:
            return self.action_verifier.verify(
                action, before_observation, after_observation
            )
        return self.platform.verify(action, before_observation, after_observation)

    async def _record_grounding_sample(
        self,
        action,
        instruction,
        grounding,
        raw_result,
        before_observation,
        success,
        expected_state,
        *,
        source=None,
        screenshot_path=None,
    ):
        if self.dataset_collector is None:
            return
        point = (grounding.x, grounding.y) if grounding is not None else None
        viewport = (
            grounding.viewport if grounding is not None
            else tuple(((before_observation.metadata or {}).get("viewport") or {}).get(
                key
            ) for key in ("width", "height"))
        )
        await self._safe_collect_gui_sample(
            screen_state=before_observation.screen,
            target=(
                (
                    self.target_resolver.fixed.target_for_action(action.name)
                    if self.target_resolver else None
                )
                or (grounding.target if grounding is not None else None)
                or action.name
            ),
            instruction=instruction,
            source=source or grounding.source,
            predicted_point=point,
            clicked_point=(raw_result or {}).get("clicked_point"),
            viewport=viewport,
            confidence=grounding.confidence if grounding is not None else None,
            success=success,
            expected_state=expected_state,
            next_state=self.observation.screen if self.observation else None,
            screenshot_path=screenshot_path,
            observation=before_observation,
        )

    async def _safe_collect_gui_sample(self, **sample):
        try:
            await self.dataset_collector.record(**sample)
        except Exception as exc:
            logger.warning(
                "GUI dataset collection skipped: error_type=%s",
                type(exc).__name__, exc_info=True,
            )

    async def _observe_after_action(
        self, action, raw_result, stage, allow_vision=True
    ):
        observation_context = {}
        if isinstance(raw_result, dict):
            observation_context = {key: value for key, value in {
                "previous_extension_sequence": raw_result.get(
                    "previous_extension_sequence"
                ),
                "expected_screen": raw_result.get("expected_screen"),
            }.items() if value is not None}
        self.observation = await self._observe(
            after_action=action.name,
            expected_message=action.args.get("text") if action.name in {
                "browser.input_text", "platform.send_message", "platform.retry_send",
            } else None,
            allow_vision=allow_vision,
            **observation_context,
        )
        self.state.update_observation(self.observation)
        self._log_observation(stage, self.observation, action.name)

    @staticmethod
    def _log_observation(stage, observation, action=None):
        logger.info(
            "Observation: stage=%s action=%s screen=%s confidence=%.2f source=%s auth=%s "
            "send_state=%s composer=%s targets=%s",
            stage,
            action or "none",
            observation.screen,
            observation.screen_confidence,
            observation.perception_source,
            observation.auth_state.value,
            observation.message_send_state.value,
            observation.composer_state.value,
            len(observation.visual_targets),
        )

    async def _observe(self, **kwargs):
        """Pass verification context while supporting simple observer adapters."""
        parameters = inspect.signature(self.observer.observe).parameters.values()
        if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
            return await self.observer.observe(**kwargs)
        accepted = {item.name for item in parameters}
        return await self.observer.observe(**{
            key: value for key, value in kwargs.items() if key in accepted
        })


# Compatibility for external code written before the LangGraph migration.
AgentLoop = AgentGraph
