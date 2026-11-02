"""Control plane embedded in each agent process.

The agent runtimes keep their live state in memory - session contexts, policy
counters, metrics, whether Chrome is still attached. None of that is reachable
from another process, so a supervisor cannot pause an agent or report what it is
doing by reading the database alone. This package adds the smallest possible HTTP
surface that closes the gap, and nothing else: it never makes decisions, it only
reports state and forwards operator intent to the objects that already exist.
"""

from control.events import EventBus, AgentEvent
from control.state import RuntimeStatus

__all__ = ["EventBus", "AgentEvent", "RuntimeStatus"]
