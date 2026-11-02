"""Sleep/Wake lifecycle for idle workspaces.

Chrome is the RAM-heavy half of a workspace (dashboard_api/processes.py's
AccountProcesses owns one Chrome + one agent process per account). This
module stops only Chrome after a period with no real customer-facing
activity, and starts it again on a timer, a dashboard heartbeat, a pin, or a
manual action - the agent process, its Chrome profile and its CDP port are
never touched, so TikTok session/cookies/extension survive untouched.

Lifecycle: ACTIVE/IDLE (Chrome running) -> SLEEPING (Chrome stopped, agent
paused) -> WAKING (Chrome restarting, CDP not ready yet) -> ACTIVE again, or
RECOVERY/DEGRADED if starting Chrome or reconnecting CDP keeps failing.

All state here is in-memory and per-supervisor-process, same as
`dashboard_api.main`'s own `processes` dict - it resets to ACTIVE on a
supervisor restart, which is conservative (nothing sleeps immediately after
a restart) rather than wrong.
"""

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum


logger = logging.getLogger(__name__)


class LifecycleState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    SLEEPING = "sleeping"
    WAKING = "waking"
    RECOVERY = "recovery"
    DEGRADED = "degraded"


@dataclass
class WorkspaceLifecycle:
    state: LifecycleState = LifecycleState.ACTIVE
    # All timestamps are monotonic() - comparable across processes on the
    # same host (CLOCK_MONOTONIC is machine-wide on Linux, not per-process),
    # which is what lets this compare against the agent's own
    # AgentState.last_business_activity_at read over /control/status.
    last_business_activity_at: float = 0.0
    last_dashboard_heartbeat_at: float = 0.0
    slept_at: float = 0.0
    woke_at: float = 0.0
    wake_reason: str = ""
    recovery_attempts: int = 0
    workspace_started_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SleepWakeManager:
    def __init__(self, *, registry, processes, hub, control, config):
        self.registry = registry
        # Same dict object dashboard_api.main keeps as `processes` - shared,
        # not copied, so a workspace started/stopped there is immediately
        # visible here.
        self.processes = processes
        self.hub = hub
        # async fn(account, method, path, payload=None) -> dict, raising on
        # failure - dashboard_api.main._control, passed in rather than
        # imported to avoid a circular import.
        self.control = control
        self.cfg = config
        self._lifecycle: dict[str, WorkspaceLifecycle] = {}
        self._settle_tasks: dict[str, asyncio.Task] = {}
        self._sweep_task: asyncio.Task | None = None
        self._wake_semaphore = asyncio.Semaphore(
            max(1, int(self.cfg.get("sleep.max_concurrent_wake", 2)))
        )
        # asyncio only holds a weak reference to a task once nothing else
        # references it - a bare `asyncio.create_task(...)` whose return
        # value is discarded can be garbage-collected mid-await, silently
        # cancelling a sleep/wake/recovery transition and leaving a
        # workspace stuck (e.g. in WAKING, which every other path here
        # deliberately leaves alone as "already in flight"). `_spawn` below
        # keeps a strong reference until the task finishes on its own.
        self._background_tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ---------------------------------------------------------------- setup
    def start(self) -> None:
        # First, and whatever sleep.enabled says: turning the feature off must
        # not strand a workspace an earlier supervisor had put to sleep.
        self._spawn(self.recover_left_asleep(), name="sleep-recover-at-boot")
        if not self.cfg.get("sleep.enabled", True):
            logger.info("[sleep] disabled (sleep.enabled=false); idle sweeper not started")
            return
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop(), name="sleep-wake-sweep")

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        for task in list(self._settle_tasks.values()):
            task.cancel()
        self._settle_tasks.clear()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    def _lc(self, account_id: str) -> WorkspaceLifecycle:
        lc = self._lifecycle.get(account_id)
        if lc is None:
            lc = WorkspaceLifecycle()
            self._lifecycle[account_id] = lc
        return lc

    def reset(self, account_id: str) -> None:
        """Forget everything about this workspace's lifecycle - called when
        an operator manually starts or fully stops it, so the sweeper always
        picks back up from a clean ACTIVE baseline rather than acting on
        stale state from before the manual action.
        """
        task = self._settle_tasks.pop(account_id, None)
        if task is not None:
            task.cancel()
        self._lifecycle.pop(account_id, None)

    def snapshot(self, account_id: str) -> dict:
        lc = self._lifecycle.get(account_id)
        if lc is None:
            return {"lifecycle_state": LifecycleState.ACTIVE.value, "idle_seconds": None}
        idle_seconds = None
        if lc.state in (LifecycleState.ACTIVE, LifecycleState.IDLE):
            baseline = max(
                lc.last_business_activity_at, lc.woke_at, lc.workspace_started_at
            )
            idle_seconds = round(max(0.0, time.monotonic() - baseline), 1)
        return {
            "lifecycle_state": lc.state.value,
            "idle_seconds": idle_seconds,
            "wake_reason": lc.wake_reason,
            "recovery_attempts": lc.recovery_attempts,
        }

    # ------------------------------------------------------- operator input
    def heartbeat(self, account_id: str) -> None:
        """The dashboard's Agent page calls this every ~15s while open."""
        lc = self._lc(account_id)
        lc.last_dashboard_heartbeat_at = time.monotonic()
        if lc.state in (LifecycleState.SLEEPING, LifecycleState.DEGRADED):
            self._spawn(self.wake(account_id, reason="heartbeat"), name=f"wake-{account_id}-heartbeat")

    def set_pinned(self, account_id: str, pinned: bool):
        account = self.registry.set_pinned(account_id, pinned)
        if account is not None and pinned:
            lc = self._lc(account_id)
            if lc.state in (LifecycleState.SLEEPING, LifecycleState.DEGRADED):
                self._spawn(self.wake(account_id, reason="pin"), name=f"wake-{account_id}-pin")
        return account

    async def wake(self, account_id: str, *, reason: str) -> None:
        await self._wake_workspace(account_id, reason=reason)

    # ------------------------------------------------------------- sweeper
    async def _sweep_loop(self) -> None:
        interval = float(self.cfg.get("sleep.sweep_interval_seconds", 30))
        while True:
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[sleep] sweep tick failed")
            await asyncio.sleep(interval)

    async def _sweep_once(self) -> None:
        idle_threshold = float(self.cfg.get("sleep.idle_threshold_seconds", 1800))
        heartbeat_grace = float(self.cfg.get("sleep.heartbeat_grace_seconds", 45))
        wake_poll_interval = float(self.cfg.get("sleep.wake_poll_interval_seconds", 300))
        now = time.monotonic()

        for account_id, managed in list(self.processes.items()):
            account = self.registry.get(account_id)
            if account is None:
                continue
            lc = self._lc(account_id)

            if lc.state in (LifecycleState.ACTIVE, LifecycleState.IDLE):
                if not managed.chrome.running:
                    # Chrome not running for a reason outside this manager's
                    # control (operator did a manual "Stop + Chrome", or
                    # Start with_chrome=False) - not ours to touch.
                    continue
                if account.pinned:
                    lc.state = LifecycleState.ACTIVE
                    continue
                if now - lc.last_dashboard_heartbeat_at < heartbeat_grace:
                    lc.state = LifecycleState.ACTIVE
                    continue
                await self._refresh_activity(account, lc)
                idle_for = now - max(
                    lc.last_business_activity_at, lc.woke_at, lc.workspace_started_at
                )
                if idle_for >= idle_threshold:
                    self._spawn(self._sleep_workspace(account_id), name=f"sleep-{account_id}")
                elif idle_for >= idle_threshold / 2:
                    lc.state = LifecycleState.IDLE
                else:
                    lc.state = LifecycleState.ACTIVE

            elif lc.state == LifecycleState.SLEEPING:
                if now - lc.slept_at >= wake_poll_interval:
                    self._spawn(self._wake_workspace(account_id, reason="poll"), name=f"wake-{account_id}-poll")

            # WAKING/RECOVERY/DEGRADED: left alone here - WAKING and RECOVERY
            # are already being driven by an in-flight task, and DEGRADED
            # waits for an explicit operator action (heartbeat/pin/manual
            # wake), not another automatic retry.

    async def _refresh_activity(self, account, lc: WorkspaceLifecycle) -> dict | None:
        try:
            status = await self.control(account, "GET", "/control/status")
        except Exception:  # noqa: BLE001 - agent unreachable: activity unknown this tick
            return None
        value = status.get("last_business_activity_at")
        if isinstance(value, (int, float)) and value > lc.last_business_activity_at:
            lc.last_business_activity_at = float(value)
        return status

    async def recover_left_asleep(self) -> list[str]:
        """Wake every workspace a previous supervisor put to sleep.

        Lifecycle state lives in this process's memory only. After a restart a
        workspace that was asleep - its agent paused with reason auto_sleep,
        its Chrome stopped - reads as ACTIVE, and nothing would ever wake it:
        the sweeper skips a workspace whose Chrome is not running, a dashboard
        heartbeat only wakes SLEEPING/DEGRADED, and with sleep disabled there
        is no sweeper at all. A pause an operator chose (any other reason) is
        left alone.
        """
        woken = []
        for account_id in list(self.processes):
            account = self.registry.get(account_id)
            if account is None:
                continue
            try:
                status = await self.control(account, "GET", "/control/status")
            except Exception:  # noqa: BLE001 - reachability probe
                continue  # not running or not reachable: nothing asleep to wake
            if not status.get("paused") or status.get("pause_reason") != "auto_sleep":
                continue
            logger.warning("[wake] %s was left asleep by a previous supervisor; waking", account_id)
            self._lc(account_id).state = LifecycleState.SLEEPING
            await self._wake_workspace(account_id, reason="boot")
            woken.append(account_id)
        return woken

    # ------------------------------------------------------------- SLEEP
    async def _sleep_workspace(self, account_id: str) -> None:
        lc = self._lc(account_id)
        async with lc.lock:
            if lc.state not in (LifecycleState.ACTIVE, LifecycleState.IDLE):
                return  # another trigger already moved this workspace on
            account = self.registry.get(account_id)
            managed = self.processes.get(account_id)
            if account is None or managed is None or not managed.chrome.running:
                return
            if account.pinned:
                return
            heartbeat_grace = float(self.cfg.get("sleep.heartbeat_grace_seconds", 45))
            if time.monotonic() - lc.last_dashboard_heartbeat_at < heartbeat_grace:
                return

            try:
                status = await self.control(account, "GET", "/control/status")
            except Exception:  # noqa: BLE001 - reachability probe
                return  # agent unreachable - unknown auth/task state, do not sleep blind
            if status.get("current_task_id"):
                return  # task in flight - the next sweep tick will retry
            if status.get("auth_state") != "LOGGED_IN":
                # Not stably logged in (AUTH_REQUIRED/SECURITY_CHALLENGE/
                # TEMPORARY_ERROR/UNKNOWN) - possibly a human mid-login flow
                # (Google/FedCM chooser) at this exact moment. Sleeping here
                # would yank Chrome out from under them, and the agent's own
                # auth-wait loop (main.py's _wait_for_authentication) is not
                # pause-aware, so it would keep hammering the now-dead CDP
                # port every few seconds instead of backing off. Only ever
                # sleep a workspace that is stably logged in.
                logger.debug(
                    "[sleep] %s: not sleeping, auth_state=%s",
                    account_id, status.get("auth_state"),
                )
                return

            try:
                await self.control(account, "POST", "/control/pause", {"reason": "auto_sleep"})
            except Exception as exc:
                logger.warning(
                    "[sleep] %s: pause failed (%s); skipping this tick",
                    account_id, type(exc).__name__, exc_info=True,
                )
                return

            for _ in range(5):
                try:
                    status = await self.control(account, "GET", "/control/status")
                except Exception:  # noqa: BLE001 - reachability probe
                    break
                if not status.get("current_task_id"):
                    break
                await asyncio.sleep(2.0)

            await managed.sleep_chrome()
            lc.state = LifecycleState.SLEEPING
            lc.slept_at = time.monotonic()
            self.hub.emit(account_id, "agent.status", status="sleeping")
            logger.info("[sleep] %s -> SLEEPING", account_id)

    # -------------------------------------------------------------- WAKE
    async def _wake_workspace(self, account_id: str, *, reason: str) -> None:
        lc = self._lc(account_id)
        async with lc.lock:
            if lc.state in (LifecycleState.SLEEPING, LifecycleState.DEGRADED):
                pass
            elif lc.state in (LifecycleState.ACTIVE, LifecycleState.IDLE):
                if reason == "heartbeat":
                    lc.last_dashboard_heartbeat_at = time.monotonic()
                return  # already awake, nothing to do
            else:
                return  # WAKING/RECOVERY - a transition is already in flight

            account = self.registry.get(account_id)
            managed = self.processes.get(account_id)
            if account is None or managed is None:
                return

            previously_degraded = lc.state == LifecycleState.DEGRADED
            lc.state = LifecycleState.WAKING
            self.hub.emit(account_id, "agent.status", status="waking", wake_reason=reason)

            if reason == "poll":
                async with self._wake_semaphore:
                    await self._do_wake(account, managed, lc, reason, previously_degraded)
            else:
                await self._do_wake(account, managed, lc, reason, previously_degraded)

    async def _do_wake(self, account, managed, lc: WorkspaceLifecycle, reason: str,
                        previously_degraded: bool) -> None:
        if reason == "poll":
            jitter = float(self.cfg.get("sleep.wake_jitter_seconds", 30))
            if jitter > 0:
                await asyncio.sleep(random.uniform(0, jitter))

        try:
            await managed.start_chrome()
        except Exception:
            logger.exception("[wake] %s: start_chrome failed", account.id)
            await self._enter_recovery(account, lc)
            return

        if not await self._wait_for_cdp(account):
            logger.warning("[wake] %s: CDP not ready within timeout", account.id)
            await self._enter_recovery(account, lc)
            return

        try:
            await self.control(account, "POST", "/control/resume")
        except Exception:
            logger.exception("[wake] %s: resume failed", account.id)
            await self._enter_recovery(account, lc)
            return

        lc.state = LifecycleState.ACTIVE
        lc.woke_at = time.monotonic()
        lc.wake_reason = reason
        lc.recovery_attempts = 0
        self.hub.emit(account.id, "agent.status", status="active", wake_reason=reason)
        logger.info("[wake] %s -> ACTIVE (reason=%s)", account.id, reason)
        if previously_degraded:
            self.hub.emit(account.id, "workspace.recovered")

        if reason == "poll":
            settle_seconds = float(self.cfg.get("sleep.wake_settle_seconds", 60))
            activity_at_wake = lc.last_business_activity_at
            self._settle_tasks[account.id] = asyncio.create_task(
                self._settle_after_poll_wake(account.id, activity_at_wake, settle_seconds)
            )

    async def _wait_for_cdp(self, account) -> bool:
        timeout = float(self.cfg.get("sleep.wake_cdp_timeout_seconds", 10))
        poll = max(0.05, float(self.cfg.get("sleep.wake_poll_seconds", 0.5)))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", account.cdp_port), timeout=poll
                )
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(poll)
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        return False

    async def _enter_recovery(self, account, lc: WorkspaceLifecycle) -> None:
        lc.recovery_attempts += 1
        max_retries = int(self.cfg.get("sleep.recovery_max_retries", 3))
        if lc.recovery_attempts > max_retries:
            lc.state = LifecycleState.DEGRADED
            self.hub.emit(
                account.id, "workspace.degraded",
                detail=f"wake failed {lc.recovery_attempts} times in a row",
            )
            logger.error(
                "[wake] %s -> DEGRADED after %s failed attempts; needs operator action",
                account.id, lc.recovery_attempts,
            )
            return
        lc.state = LifecycleState.RECOVERY
        backoff = min(60.0, float(2 ** lc.recovery_attempts)) + random.uniform(0, 1.0)
        self.hub.emit(account.id, "agent.status", status="recovery", attempt=lc.recovery_attempts)
        logger.warning(
            "[wake] %s -> RECOVERY attempt=%s backoff=%.1fs",
            account.id, lc.recovery_attempts, backoff,
        )
        self._spawn(self._retry_wake_after(account.id, backoff), name=f"wake-retry-{account.id}")

    async def _retry_wake_after(self, account_id: str, backoff: float) -> None:
        await asyncio.sleep(backoff)
        lc = self._lifecycle.get(account_id)
        if lc is None or lc.state != LifecycleState.RECOVERY:
            return
        # Re-enter through the same SLEEPING gate _wake_workspace checks -
        # this retry is exactly a fresh wake attempt.
        lc.state = LifecycleState.SLEEPING
        await self._wake_workspace(account_id, reason="poll")

    # ------------------------------------------------------------- settle
    async def _settle_after_poll_wake(
        self, account_id: str, activity_at_wake: float, settle_seconds: float,
    ) -> None:
        await asyncio.sleep(settle_seconds)
        lc = self._lifecycle.get(account_id)
        if lc is None or lc.state not in (LifecycleState.ACTIVE, LifecycleState.IDLE):
            return
        account = self.registry.get(account_id)
        if account is None or account.pinned:
            return
        heartbeat_grace = float(self.cfg.get("sleep.heartbeat_grace_seconds", 45))
        if time.monotonic() - lc.last_dashboard_heartbeat_at < heartbeat_grace:
            return
        await self._refresh_activity(account, lc)
        if lc.last_business_activity_at > activity_at_wake:
            return  # real work happened during the settle window - stay awake
        await self._sleep_workspace(account_id)
