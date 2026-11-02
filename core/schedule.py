"""Operating hours: when the agent is allowed to start new work.

Nothing in the codebase expressed this before, so it is a new gate rather than a
new config toggle. It is deliberately narrow: it decides whether *new* work may
begin, and never interrupts work already running. Stopping mid-conversation to
respect a closing time would leave a customer with half an answer.

Times are local to the machine the agent runs on, which is the same machine the
operator reads the clock on.
"""

import logging
from datetime import datetime, time


logger = logging.getLogger(__name__)

# Monday=0 .. Sunday=6, matching datetime.weekday().
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_time(value, fallback: time) -> time:
    try:
        hour, _, minute = str(value).partition(":")
        return time(int(hour), int(minute or 0))
    except (TypeError, ValueError):
        return fallback


class OperatingHours:
    """Answers one question: may the agent start something right now?"""

    def __init__(self, *, enabled=False, start="00:00", end="23:59", days=None,
                 timezone_note=""):
        self.enabled = bool(enabled)
        self.start = _parse_time(start, time(0, 0))
        self.end = _parse_time(end, time(23, 59))
        # Empty means every day.
        self.days = tuple(str(day).lower()[:3] for day in (days or ()))
        self.timezone_note = timezone_note

    @classmethod
    def from_config(cls, config) -> "OperatingHours":
        return cls(
            enabled=config.get("messaging.schedule.enabled", False),
            start=config.get("messaging.schedule.start", "00:00"),
            end=config.get("messaging.schedule.end", "23:59"),
            days=config.get("messaging.schedule.days", []) or [],
        )

    def is_open(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return True
        moment = now or datetime.now()
        if self.days and DAY_NAMES[moment.weekday()] not in self.days:
            return False
        current = moment.time()
        if self.start <= self.end:
            return self.start <= current <= self.end
        # A window that wraps midnight (e.g. 22:00 -> 06:00) is open on both
        # sides of it, not closed all day.
        return current >= self.start or current <= self.end

    def describe(self) -> str:
        if not self.enabled:
            return "always open"
        days = ",".join(self.days) if self.days else "every day"
        return f"{self.start:%H:%M}-{self.end:%H:%M} {days}"

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "start": f"{self.start:%H:%M}",
            "end": f"{self.end:%H:%M}",
            "days": list(self.days),
            "open_now": self.is_open(),
            "description": self.describe(),
        }
