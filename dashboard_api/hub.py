"""Fan-in of every agent's SSE stream, fan-out to every browser WebSocket.

The supervisor is the only component that sees all accounts, so it is also the
only place that can turn N per-agent streams into one dashboard feed. It keeps
the last known status per account so a browser that connects late renders
immediately instead of waiting for the next change.

A dropped SSE connection is itself information - it is exactly the "agent /
bridge disconnected" notification the dashboard needs - so it is published as an
event rather than retried silently.
"""

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

import httpx

from control import auth as control_auth


logger = logging.getLogger(__name__)

RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 15.0

# Only these types describe a *current state* that a late browser needs
# replayed to render correctly. Everything else is a point-in-time fact
# ("this message failed", "this task finished"): replaying it makes a browser
# that reloads minutes later treat an old event as if it just happened - a
# failed-send bubble the operator already dismissed came back on every
# refresh, forever, because the event was still the newest of its type.
REPLAY_TYPES = frozenset({"agent.status", "health.changed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_of(event: dict[str, Any]) -> str:
    return str(event.get("account_id") or "")


class EventHub:
    def __init__(self):
        # queue -> "may this subscriber see events for this account_id?".
        # A callable rather than a captured set, so that a permission revoked
        # while a browser is connected takes effect on the next event: the
        # /api/events handler refreshes the set the callable closes over.
        self._clients: dict[asyncio.Queue, Callable[[str], bool]] = {}
        self._followers: dict[str, asyncio.Task] = {}
        self._connected: dict[str, bool] = {}
        # Latest state event per (account, type), replayed to new browsers.
        self._latest: dict[str, dict[str, Any]] = {}
        # Optional sink for events worth persisting (notifications). Kept as a
        # plain attribute so the hub has no dependency on what consumes it.
        self.on_event: Any = None

    # ------------------------------------------------------- browser side
    def subscribe(
        self, visible: Callable[[str], bool] | None = None
    ) -> asyncio.Queue:
        """Register a browser queue, optionally scoped to some accounts.

        `visible(account_id) -> bool` gates both the replay below and every
        later `publish`. The default says yes to everything, which is what an
        admin connection and the hub's own tests want.
        """
        allow = visible if visible is not None else (lambda _account_id: True)
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        for event in self._latest.values():
            # The replay is per-account state, so it needs the same filter as
            # live events - without it a fresh connection leaked one event per
            # account before a single new thing had happened.
            if not allow(_account_of(event)):
                continue
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        self._clients[queue] = allow
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._clients.pop(queue, None)

    def publish(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type in REPLAY_TYPES:
            self._latest[f"{event.get('account_id', '')}:{event_type}"] = event
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as exc:
                logger.warning("[hub] event sink failed: %s", type(exc).__name__, exc_info=True)
        account_id = _account_of(event)
        if not account_id:
            # Every emit() call site passes a real account id, so this means a
            # new code path forgot to. Admin-only is the fail-closed reading,
            # and the warning is here because the alternative - a silent leak
            # to every connected browser - is exactly what this filter exists
            # to prevent.
            logger.warning(
                "[hub] event without account_id type=%s - delivered to admins only",
                event_type or "?",
            )
        for queue, allow in list(self._clients.items()):
            if not allow(account_id):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait(event)

    def emit(self, account_id: str, event_type: str, **payload: Any) -> None:
        self.publish({
            "account_id": account_id, "type": event_type,
            "ts": _now(), "payload": payload,
        })

    # --------------------------------------------------------- agent side
    def follow(self, account_id: str, control_port: int) -> None:
        if account_id in self._followers and not self._followers[account_id].done():
            return
        self._followers[account_id] = asyncio.create_task(
            self._follow(account_id, control_port), name=f"follow-{account_id}"
        )

    def unfollow(self, account_id: str) -> None:
        task = self._followers.pop(account_id, None)
        if task is not None:
            task.cancel()
        self._connected.pop(account_id, None)

    def is_following(self, account_id: str) -> bool:
        """Is this workspace's agent expected to be reachable? A manual Stop
        or a delete unfollows it; a crash does not."""
        task = self._followers.get(account_id)
        return task is not None and not task.done()

    def is_connected(self, account_id: str) -> bool:
        return bool(self._connected.get(account_id))

    async def _follow(self, account_id: str, control_port: int) -> None:
        url = f"http://127.0.0.1:{control_port}/control/events"
        delay = RECONNECT_MIN_SECONDS
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    # Re-read on every reconnect: a restarted agent mints a
                    # new token, and this loop outlives it.
                    headers = control_auth.headers_for(control_port)
                    async with client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        self._mark(account_id, True)
                        delay = RECONNECT_MIN_SECONDS
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                self._forward(account_id, line[5:].strip())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - expected whenever the agent is down; reconnects with backoff
                if self._connected.get(account_id):
                    logger.info(
                        "[hub] %s stream lost (%s)", account_id, type(exc).__name__
                    )
                self._mark(account_id, False)
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_SECONDS, delay * 2)

    def _forward(self, account_id: str, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        event.setdefault("account_id", account_id)
        self.publish(event)

    def _mark(self, account_id: str, connected: bool) -> None:
        if self._connected.get(account_id) == connected:
            return
        self._connected[account_id] = connected
        self.emit(
            account_id, "health.changed",
            component="control", ok=connected,
            detail="connected" if connected else "agent not reachable",
        )

    async def close(self) -> None:
        for task in self._followers.values():
            task.cancel()
        if self._followers:
            await asyncio.gather(*self._followers.values(), return_exceptions=True)
        self._followers.clear()
        # Each browser's /api/events handler blocks forever on `queue.get()`;
        # without a wakeup here it never notices shutdown, and uvicorn's
        # graceful stop waits on that still-open connection until systemd's
        # TimeoutStopSec gives up and SIGKILLs the process. None is the
        # sentinel the websocket loop below treats as "stop".
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
