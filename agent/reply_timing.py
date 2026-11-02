"""Where the time goes while one customer message is answered.

One `ReplyTimer` follows one conversation through the inbox scan: opening the
thread, holding until the customer stops typing, the reply pipeline (and its
own analyze / tool / generate / validate steps), the pre-send re-read, the
send and the DOM read-back that verifies it. A phase that runs twice - the
rewrite after a refused draft, a second pass after the inbound changed - adds
to the same total, so the numbers always sum to time actually spent.

Measured with the agent's monotonic clock. TikTok rarely exposes when a
message was sent, so "how long the customer waited" can only start from the
moment the agent first read it: `total` is time from the open click (or from
reading an already-open thread) to the verified send.
"""

from contextlib import contextmanager
from time import monotonic


# The steps a reply row stores as their own columns, in the order they run.
# Anything else measured (the pipeline's internals) goes into `detail`.
STEP_PHASES = ("open", "hold", "reply", "recheck", "send", "verify")


class ReplyTimer:
    def __init__(self, clock=monotonic):
        self._clock = clock
        self.started = clock()
        self.phases: dict[str, float] = {}
        # How many unanswered customer messages the reply covered.
        self.burst_size = 0

    @contextmanager
    def phase(self, name: str):
        started = self._clock()
        try:
            yield
        finally:
            self.add(name, self._clock() - started)

    def add(self, name: str, seconds: float) -> None:
        self.phases[name] = self.phases.get(name, 0.0) + max(0.0, float(seconds))

    def has(self, name: str) -> bool:
        return name in self.phases

    def elapsed(self) -> float:
        return self._clock() - self.started

    def milliseconds(self) -> dict[str, int]:
        """Every phase plus `total`, rounded to whole milliseconds."""
        result = {name: round(seconds * 1000) for name, seconds in self.phases.items()}
        result["total"] = round(self.elapsed() * 1000)
        return result

    def summary(self) -> str:
        """`open=0.41s hold=10.2s ...` for one log line, steps first."""
        ordered = [name for name in STEP_PHASES if name in self.phases]
        ordered += sorted(name for name in self.phases if name not in STEP_PHASES)
        parts = [f"{name}={self.phases[name]:.2f}s" for name in ordered]
        parts.append(f"total={self.elapsed():.2f}s")
        return " ".join(parts)
