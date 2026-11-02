"""In-process event bus: agent internals -> SSE subscribers.

Publishing must never slow the agent down or fail it. A subscriber that stops
reading gets its oldest events dropped rather than applying backpressure to the
runtime, and a publish with no subscribers is free.

Publisher and subscriber no longer share one event loop. The control server
runs on its own loop in its own thread, so that an operator's takeover is not
queued behind whatever the agent is doing, which means the SSE subscriber
queues are created on the control loop while most events are published from
the agent loop. `asyncio.Queue` is not thread-safe, so each subscriber is
remembered together with the loop it belongs to and is fed through
`call_soon_threadsafe` when the publisher is somewhere else. Same-loop
publishing stays a direct `put_nowait`.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger(__name__)

# Per-subscriber buffer. A dashboard that pauses for a few seconds catches up;
# one that is gone loses the middle of the stream instead of stalling the agent.
QUEUE_SIZE = 256


@dataclass(frozen=True)
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "account_id": self.account_id,
            "ts": self.ts,
            "payload": self.payload,
        }


class EventBus:
    """Fan-out of agent events to any number of SSE subscribers."""

    def __init__(self, account_id: str = ""):
        self.account_id = account_id
        # queue -> the loop that queue was created on. Feeding a queue from
        # another loop has to go through that loop, never straight in.
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop | None] = {}
        # The most recent state per event type, replayed to a new subscriber so
        # a dashboard that connects late still renders the current picture
        # instead of an empty screen until something happens to change.
        self._latest: dict[str, AgentEvent] = {}

    def publish(self, event_type: str, **payload: Any) -> None:
        """Fire-and-forget. Safe from any thread, loop or sync code."""
        event = AgentEvent(
            type=event_type, payload=payload, account_id=self.account_id
        )
        self._latest[event_type] = event
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for queue, loop in list(self._subscribers.items()):
            if loop is None or loop is current:
                self._offer(queue, event)
                continue
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # The subscriber's loop is closed; its SSE response is over
                # and `unsubscribe` either already ran or is about to.
                self._subscribers.pop(queue, None)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: "AgentEvent") -> None:
        """Put one event in, dropping the oldest rather than blocking."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()           # drop the oldest, keep the newest
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        for event in self._latest.values():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A subscriber created outside a loop (a test, sync code) can only
            # be fed directly; there is no loop to hand the event to.
            loop = None
        self._subscribers[queue] = loop
        logger.debug("[events] subscriber added (%s total)", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
