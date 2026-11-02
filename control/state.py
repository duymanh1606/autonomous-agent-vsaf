"""One status snapshot, assembled from whatever the host runtime exposes.

The two runtimes are structurally different - `main.py` owns a database, a
browser and a LangGraph loop, and is the only runtime there is (the older
extension runtime was removed); sessions are no
database at all - so this deliberately probes with `getattr` instead of
requiring one shape. A field that a runtime cannot answer is reported as
`None`, never guessed.
"""

import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable


logger = logging.getLogger(__name__)

# Auth and model probes reach the network; a dashboard polling health must not
# turn into a request amplifier against TikTok or Gemini.
HEALTH_CACHE_SECONDS = 15.0


@dataclass
class RuntimeStatus:
    """Live status of one agent process."""

    account_id: str = ""
    runtime: str = "unknown"          # "langgraph" | "extension"
    started_at: float = field(default_factory=monotonic)
    agent: Any = None                 # main.py Agent, when present
    controller: Any = None            # UIController, when present
    health_probe: Callable | None = None

    _health_cache: dict = field(default_factory=dict)
    _health_at: float = 0.0

    # ------------------------------------------------------------- status
    def snapshot(self) -> dict[str, Any]:
        state = getattr(self.agent, "state", None)
        sessions = getattr(self.controller, "sessions", None)
        return {
            "account_id": self.account_id,
            "runtime": self.runtime,
            "uptime_seconds": round(monotonic() - self.started_at, 1),
            "running": bool(getattr(state, "running", True)),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "current_task_id": getattr(state, "current_task_id", None),
            "current_screen": getattr(state, "current_screen", None),
            "last_action": getattr(state, "last_action", None),
            "action_count": getattr(state, "action_count", None),
            # monotonic() timestamp - comparable to the supervisor's own
            # time.monotonic() on the same host (dashboard_api's idle
            # sweeper polls this to decide when a workspace is truly idle).
            "last_business_activity_at": getattr(state, "last_business_activity_at", None),
            "auth_state": self._enum(getattr(state, "auth_state", None)),
            "security_challenge": getattr(state, "security_challenge", None),
            "sessions": [
                {
                    "session_id": session.session_id,
                    "state": session.machine.state.value,
                    "conversation": session.pending_conversation,
                    "human_reason": session.human_reason,
                }
                for session in (sessions or {}).values()
            ],
        }

    # -------------------------------------------------------------- pause
    @property
    def paused(self) -> bool:
        state = getattr(self.agent, "state", None)
        if state is not None:
            return bool(getattr(state, "paused", False))
        if self.controller is not None:
            return bool(getattr(self.controller, "paused", False))
        return False

    @property
    def pause_reason(self) -> str:
        state = getattr(self.agent, "state", None)
        if state is not None:
            return str(getattr(state, "pause_reason", "") or "")
        return str(getattr(self.controller, "pause_reason", "") or "")

    @property
    def in_flight(self) -> bool:
        """True while a task is still running, paused or not.

        Pausing stops the loop claiming the *next* task; whatever is already
        mid-flight runs to completion, because abandoning a half-finished UI
        action is how a customer gets answered twice or not at all. Anything
        that wants to know when the agent is genuinely idle - a drain before a
        restart, a dashboard that must not claim it stopped - asks this.
        """
        state = getattr(self.agent, "state", None)
        return bool(getattr(state, "current_task_id", None))

    def set_paused(self, paused: bool, reason: str = "") -> dict[str, Any]:
        state = getattr(self.agent, "state", None)
        if state is not None:
            state.paused = bool(paused)
            state.pause_reason = reason if paused else ""
        if self.controller is not None:
            self.controller.paused = bool(paused)
            self.controller.pause_reason = reason if paused else ""
        # A LangGraph task already in flight runs to completion, so "paused" is
        # not yet true from the operator's point of view. Say so rather than
        # letting a dashboard claim the agent stopped when it has not.
        return {
            "paused": self.paused,
            "pending": bool(paused and self.in_flight),
            "reason": self.pause_reason,
        }

    # ------------------------------------------------------------- health
    async def health(self) -> dict[str, Any]:
        now = monotonic()
        if self._health_cache and now - self._health_at < HEALTH_CACHE_SECONDS:
            return self._health_cache
        if self.health_probe is None:
            result = {"runtime": {"ok": True, "detail": self.runtime}}
        else:
            try:
                result = await self.health_probe()
            except Exception as exc:
                logger.warning(
                    "[control] health probe failed error_type=%s", type(exc).__name__,
                    exc_info=True,
                )
                result = {"probe": {"ok": False, "detail": type(exc).__name__}}
        self._health_cache = result
        self._health_at = now
        return result

    @staticmethod
    def _enum(value):
        return getattr(value, "value", value)
