from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from models.account import require_account_id


# What every routine task (inbox scan, auto-reply, backfill, ...) runs at.
# A staff reply is submitted far above it (dashboard_api/main.py uses 100) so
# it is claimed ahead of anything queued, and a long-running task compares
# against this to tell whether it is standing in somebody's way.
DEFAULT_TASK_PRIORITY = 5


class Task(BaseModel):

    id: str

    # Required, with no default. A task is work done *as* one account; a task
    # that does not name its account cannot be checked against the runtime
    # executing it, and an unchecked task is how a reply reaches the wrong
    # customer. Guessing an account here is explicitly not allowed.
    account_id: str

    capability: str

    platform: str

    priority: int = DEFAULT_TASK_PRIORITY

    payload: dict[str, Any] = Field(default_factory=dict)

    status: str = "pending"

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value):
        return require_account_id(value)


class TaskClaimState(str, Enum):
    """Result of an atomic task claim attempt."""

    GRANTED = "GRANTED"
    DUPLICATE = "DUPLICATE"
    LOCKED = "LOCKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class TaskClaim:
    state: TaskClaimState
    task: Task | None = None
    reason: str = ""
    owner_id: str = ""
    attempt_count: int = 0

    @property
    def granted(self) -> bool:
        return self.state in {TaskClaimState.GRANTED, TaskClaimState.EXPIRED}
