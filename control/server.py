"""The HTTP control surface embedded in an agent process.

Runs on its own event loop in its own thread, next to the runtime it controls.
It binds loopback only and carries no authentication, matching the extension
bridge's local-only posture.

It used to share the agent's event loop, which made every operator action
queue behind whatever the agent was doing - and the agent does things that do
not yield, such as probing a video with `ffprobe` (up to 5s). Taking over a
conversation is two indexed queries, so an operator waiting seconds for it was
waiting on the wrong thing entirely.

Crossing back into the agent is explicit, and there are only two ways:

* `_on_agent_loop` runs an agent-touching coroutine on the agent's loop and
  awaits it with a timeout, so a blocked agent becomes a 504 instead of a hung
  request. Used by health, task submission and settings - everything that
  reads or mutates the agent's own objects (its browser, its database
  connection, its asyncio primitives).
* `_call_on_agent_loop` hands a plain callback over without waiting. Used by
  shutdown, whose `asyncio.Event` belongs to that loop.

Everything else stays on this thread on purpose. Pause, resume and drain only
read and write attributes, and conversation takeover/release run on this
server's own database connection (`storage.SecondaryConnection`, wired in
main.py), so the one thing an operator needs during an incident never waits
for the agent at all.

With no agent loop attached - the tests, and any runtime that starts this
server without `start()` - every call above runs inline, exactly as before.

It deliberately does NOT own business logic: pause flips a flag the graph loop
already checks, task submission reuses `TaskRepository.submit`, and settings go
through an explicit whitelist. Anything it cannot do safely it reports as
unsupported rather than approximating.
"""

import asyncio
import json
import logging
import threading
from time import monotonic
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from control import auth
from control.events import EventBus
from control.state import RuntimeStatus


logger = logging.getLogger(__name__)

# How often a drain re-checks whether the in-flight task has finished. Cheap:
# it reads one attribute off the agent's state object.
DRAIN_POLL_SECONDS = 0.25

# How long a call that has to run on the agent's loop waits for it. Long
# enough for the agent's longest uninterruptible step (staging a video is
# capped at 60s), short enough that the caller gets an answer rather than a
# socket that never replies. The supervisor's own client gives up at 10s
# (`CONTROL_TIMEOUT` in dashboard_api/main.py), so in practice it sees that
# first - this is the backstop that stops the request living forever here.
AGENT_CALL_TIMEOUT_SECONDS = 65.0

# How often such a call checks whether the agent's loop has answered yet.
AGENT_CALL_POLL_SECONDS = 0.02

# How long `start()` waits for uvicorn to actually accept connections before
# logging that it is listening. Without it the log line is a guess.
LISTEN_TIMEOUT_SECONDS = 10.0


class DrainRequest(BaseModel):
    # Long enough for the worst honest case - a burst hold plus the reply it
    # was waiting to send - and bounded so a script cannot hang on it.
    timeout_seconds: float = Field(60.0, ge=0, le=300)
    reason: str = ""


class PauseRequest(BaseModel):
    reason: str = ""


class TaskRequest(BaseModel):
    capability: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 5
    platform: str = "tiktok_web"
    # A caller-supplied id makes submission idempotent (the dashboard's
    # manual-send flow relies on this for client_message_id dedup).
    task_id: str | None = None


class SettingsRequest(BaseModel):
    values: dict[str, Any]


class TakeoverRequest(BaseModel):
    operator_id: str = "local_operator"


class ControlServer:
    """FastAPI app + uvicorn server, started as a task by the host runtime."""

    def __init__(
        self,
        status: RuntimeStatus,
        events: EventBus,
        *,
        host: str = "127.0.0.1",
        port: int = 8900,
        task_submitter: Callable | None = None,
        settings_applier: Callable | None = None,
        shutdown: Callable | None = None,
        conversation_control=None,
        token: str | None = None,
    ):
        self.status = status
        self.events = events
        self.host = host
        self.port = port
        self.task_submitter = task_submitter
        self.settings_applier = settings_applier
        self.shutdown = shutdown
        # Per-conversation AUTO/WAITING_HUMAN/HUMAN state. Optional, like
        # `task_submitter`: an unsupported runtime reports 501 instead of
        # approximating.
        self.conversation_control = conversation_control
        # Required on every route (see control/auth.py).
        # Minted here rather than defaulted to "no auth": a caller that
        # forgets to pass one gets a locked server, not an open one.
        self.token = token or auth.new_token()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        # The agent's loop, captured in `start()`. `None` means single-loop
        # mode: every agent-touching call runs inline, which is what the tests
        # exercise and what a runtime that never calls `start()` gets.
        self._agent_loop: asyncio.AbstractEventLoop | None = None
        self._listening = threading.Event()
        self.app = self._build_app()

    # ------------------------------------------------- crossing the threads
    async def _on_agent_loop(self, factory, *, timeout=None):
        """Run an agent-touching coroutine where the agent's objects live.

        This server has its own loop so an operator's call is not queued
        behind the agent - but its browser, its database connection and its
        asyncio primitives belong to the agent's loop and must be touched
        there. Awaiting the future leaves this loop free meanwhile, and the
        timeout turns a blocked agent into an answer rather than a hung
        request.
        """
        # Read at call time, not bound as a default: one source of truth, and
        # a test can shorten it.
        timeout = AGENT_CALL_TIMEOUT_SECONDS if timeout is None else timeout
        loop = self._agent_loop
        if loop is None or loop.is_closed():
            return await factory()
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        # Polled rather than `wait_for(wrap_future(...))`: cancelling a
        # cross-loop future that is wedged in a step which never yields
        # surfaces as `CancelledError` instead of a timeout, and the caller
        # then gets a dropped connection rather than an answer.
        deadline = monotonic() + timeout
        try:
            while not future.done():
                if monotonic() >= deadline:
                    future.cancel()
                    logger.warning(
                        "[control] the agent loop did not answer within %.0fs; "
                        "it is inside a step that does not yield", timeout,
                    )
                    raise HTTPException(
                        status_code=504,
                        detail=(
                            "the agent is busy in a step that cannot be "
                            "interrupted; it did not answer in time"
                        ),
                    )
                await asyncio.sleep(AGENT_CALL_POLL_SECONDS)
        except asyncio.CancelledError:
            # The client hung up: stop the work we asked the agent to do.
            future.cancel()
            raise
        return future.result()

    def _call_on_agent_loop(self, callback, *args) -> None:
        """Hand a plain callback to the agent's loop without waiting for it."""
        loop = self._agent_loop
        if loop is None or loop.is_closed():
            callback(*args)
            return
        loop.call_soon_threadsafe(callback, *args)

    # ------------------------------------------------------------- routes
    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Agent Control", docs_url=None, redoc_url=None)
        # No CORS middleware, deliberately. It used to allow every origin on
        # the reasoning that loopback exposes nothing beyond this machine - but
        # a web page open in any browser here is on this machine, and with
        # `*` it could POST a send_message task or read customer text off
        # /control/events. Without CORS headers a browser will not let a page
        # read a response or send a JSON POST at all; the token below is what
        # stops everything that is not a browser.

        @app.middleware("http")
        async def require_token(request: Request, call_next):
            if not auth.token_matches(request.headers.get(auth.TOKEN_HEADER), self.token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "missing or wrong control token"},
                )
            return await call_next(request)

        @app.get("/control/status")
        async def read_status():
            return self.status.snapshot()

        @app.get("/control/health")
        async def read_health():
            # Probes the agent's browser and its database connection, so it
            # has to run on the agent's loop.
            checks = await self._on_agent_loop(self.status.health)
            return {"account_id": self.status.account_id, "checks": checks}

        @app.post("/control/pause")
        async def pause(request: PauseRequest):
            result = self.status.set_paused(True, request.reason or "operator")
            self.events.publish("agent.paused", **result)
            logger.info("[control] paused reason=%s", result["reason"])
            return result

        @app.post("/control/drain")
        async def drain(request: DrainRequest):
            """Stop taking new work, then report once nothing is in flight.

            This is what makes restarting an agent safe to script. The agent
            owns one Chrome session and its side effects are irreversible - a
            message already on its way to a customer cannot be rolled back -
            so a deploy must not stop it mid-action. Draining is the only
            honest way to get there: refuse the next task, let the current one
            finish, then say whether it did.

            Deliberately does not resume and does not stop the process. The
            caller decides: stop the workspace, or `POST /control/resume` to
            put it back to work. A drain that timed out reports
            `drained: false`, and stopping anyway is then an explicit choice
            rather than an accident.
            """
            started = monotonic()
            paused = self.status.set_paused(True, request.reason or "drain")
            deadline = started + request.timeout_seconds
            while self.status.in_flight and monotonic() < deadline:
                await asyncio.sleep(DRAIN_POLL_SECONDS)
            waited = monotonic() - started
            drained = not self.status.in_flight
            logger.info(
                "[control] drain reason=%s drained=%s waited_seconds=%.1f "
                "timeout_seconds=%.1f",
                paused["reason"], drained, waited, request.timeout_seconds,
            )
            if not drained:
                logger.warning(
                    "[control] drain timed out with a task still running; "
                    "the agent stays paused - resume it or stop it explicitly"
                )
            return {
                "drained": drained,
                "in_flight": not drained,
                "waited_seconds": round(waited, 2),
                "paused": paused["paused"],
                "reason": paused["reason"],
            }

        @app.post("/control/resume")
        async def resume():
            result = self.status.set_paused(False)
            self.events.publish("agent.resumed", **result)
            logger.info("[control] resumed")
            return result

        @app.post("/control/tasks")
        async def submit_task(request: TaskRequest):
            if self.task_submitter is None:
                # The extension runtime has no task queue at all; saying so is
                # better than accepting work that will never run.
                raise HTTPException(
                    status_code=501,
                    detail="this runtime has no task queue",
                )
            # Writes through the agent's own repository and then wakes its
            # loop, so it runs there.
            try:
                task_id = await self._on_agent_loop(
                    lambda: self.task_submitter(
                        capability=request.capability,
                        payload=request.payload,
                        priority=request.priority,
                        platform=request.platform,
                        task_id=request.task_id,
                    )
                )
            except ValueError as exc:
                # The submitter's own refusal (e.g. a capability nothing can
                # run) is the caller's mistake, not this server's.
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            self.events.publish(
                "task.submitted", task_id=task_id, capability=request.capability
            )
            return {"task_id": task_id, "capability": request.capability}

        @app.post("/control/conversations/{conversation_id}/takeover")
        async def takeover_conversation(conversation_id: str, request: TakeoverRequest):
            if self.conversation_control is None:
                raise HTTPException(
                    status_code=501,
                    detail="this runtime has no conversation control",
                )
            started = monotonic()
            result = await self.conversation_control.takeover(
                conversation_id, request.operator_id
            )
            elapsed_ms = (monotonic() - started) * 1000
            self.events.publish(
                "conversation.takeover",
                conversation_id=conversation_id, operator_id=request.operator_id,
            )
            # `elapsed_ms` is the database work only. The supervisor logs the
            # whole call (see `_control` in dashboard_api/main.py), and the
            # difference between the two is what the request spent waiting for
            # this process: the control server shares one event loop with the
            # agent, so a synchronous stretch in the agent delays a takeover
            # even though the takeover itself is two queries.
            logger.info(
                "[control] conversation takeover conversation=%s operator=%s "
                "mode=%s elapsed_ms=%.1f",
                conversation_id, request.operator_id,
                (result or {}).get("mode", "unknown"), elapsed_ms,
            )
            return result

        @app.post("/control/conversations/{conversation_id}/release")
        async def release_conversation(conversation_id: str):
            if self.conversation_control is None:
                raise HTTPException(
                    status_code=501,
                    detail="this runtime has no conversation control",
                )
            started = monotonic()
            result = await self.conversation_control.release(conversation_id)
            elapsed_ms = (monotonic() - started) * 1000
            self.events.publish("conversation.released", conversation_id=conversation_id)
            logger.info(
                "[control] conversation released conversation=%s mode=%s elapsed_ms=%.1f",
                conversation_id, (result or {}).get("mode", "unknown"), elapsed_ms,
            )
            return result

        @app.patch("/control/settings")
        async def patch_settings(request: SettingsRequest):
            if self.settings_applier is None:
                raise HTTPException(
                    status_code=501, detail="this runtime has no mutable settings"
                )
            # Mutates the agent's live config and policy objects.
            return await self._on_agent_loop(
                lambda: self.settings_applier(request.values)
            )

        @app.post("/control/shutdown")
        async def request_shutdown():
            if self.shutdown is None:
                raise HTTPException(status_code=501, detail="shutdown not wired")
            self.events.publish("agent.stopping")
            # Sets the agent's `running` flag and an `asyncio.Event` that
            # belongs to its loop, so it is handed over rather than called
            # from this thread.
            self._call_on_agent_loop(self.shutdown)
            return {"stopping": True}

        @app.get("/control/events")
        async def stream_events():
            queue = self.events.subscribe()

            async def publisher():
                try:
                    while True:
                        event = await queue.get()
                        yield {
                            "event": event.type,
                            "data": json.dumps(event.as_dict(), ensure_ascii=False),
                        }
                finally:
                    self.events.unsubscribe(queue)

            return EventSourceResponse(publisher())

        return app

    # ---------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Serve on a thread of our own, and only claim so once it is true."""
        self._agent_loop = asyncio.get_running_loop()
        # Published before the port opens, so no caller ever finds the server
        # up but the token not yet readable.
        auth.write_token(self.port, self.token)
        config = uvicorn.Config(
            self.app, host=self.host, port=self.port,
            log_level="warning", access_log=False, lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._listening = threading.Event()
        self._thread = threading.Thread(
            target=self._serve_on_own_loop,
            name=f"control-server-{self.port}",
            daemon=True,
        )
        self._thread.start()
        listening = await asyncio.to_thread(
            self._listening.wait, LISTEN_TIMEOUT_SECONDS
        )
        if listening:
            logger.info("[control] listening on http://%s:%s", self.host, self.port)
        else:
            # Never report a port as open when it is not: an operator whose
            # takeover fails needs to know the control surface never came up.
            logger.error(
                "[control] server did not start listening on %s:%s within %.0fs",
                self.host, self.port, LISTEN_TIMEOUT_SECONDS,
            )

    def _serve_on_own_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve(loop))
        except Exception:
            logger.exception("[control] server thread stopped with an error")
        finally:
            # Any SSE subscriber queue belongs to this loop; the bus drops a
            # subscriber whose loop is gone rather than failing a publish.
            self._listening.set()          # never leave `start()` waiting
            loop.close()

    async def _serve(self, loop) -> None:
        serving = loop.create_task(self._server.serve())
        while not self._server.started and not serving.done():
            await asyncio.sleep(0.02)
        self._listening.set()
        await serving

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
            if thread.is_alive():
                logger.warning(
                    "[control] server thread did not exit within 5s; "
                    "it is a daemon and will not hold the process open"
                )
        self._server = None
        self._agent_loop = None
        auth.remove_token(self.port, self.token)
