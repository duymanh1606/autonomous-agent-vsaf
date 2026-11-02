from dataclasses import dataclass, field
from time import monotonic

from models.auth import AuthState


@dataclass
class AgentState:
    running: bool = True
    # Set by the control plane. The graph loop honours it at the next task
    # boundary, so a pause is immediate while idle but waits out a task already
    # in flight (bounded by policy.max_task_runtime_seconds). Callers that need
    # to know the difference read `pause_pending`.
    paused: bool = False
    pause_reason: str = ""
    current_task_id: str | None = None
    current_screen: str | None = None
    previous_screen: str | None = None
    last_action: str | None = None
    retry_count: int = 0
    action_count: int = 0
    task_started_at: float | None = None
    security_challenge: bool = False
    auth_state: AuthState = AuthState.UNKNOWN
    auth_blocked_until: float = 0.0
    temporary_error_count: int = 0
    auth_enforced: bool = False
    auth_probe_attempted: bool = False
    metadata: dict = field(default_factory=dict)
    # monotonic() timestamp of the last task whose capability is customer-
    # facing messaging work (core.policy.MESSAGING_CAPABILITIES) - distinct
    # from task_started_at, which tracks the *current* task regardless of
    # kind. The dashboard supervisor's idle sweeper reads this (via
    # /control/status) to decide when a workspace is truly idle, since a
    # routine inbox scan finding nothing must not count as activity.
    last_business_activity_at: float = 0.0

    def begin_task(self, task_id: str):
        self.current_task_id = task_id
        self.retry_count = 0
        self.action_count = 0
        self.task_started_at = monotonic()
        self.security_challenge = False
        self.metadata.clear()

    @property
    def task_runtime_seconds(self):
        return 0.0 if self.task_started_at is None else monotonic() - self.task_started_at

    def update_observation(self, observation):
        self.previous_screen = self.current_screen
        self.current_screen = observation.screen
        self.security_challenge = observation.security_challenge
        self.auth_state = observation.auth_state
        self.metadata["screen_confidence"] = observation.screen_confidence
        self.metadata["perception_source"] = observation.perception_source
        self.metadata["visual_targets"] = {
            target.target_id: target.model_dump()
            for target in observation.visual_targets
        }
        self.metadata["message_send_state"] = observation.message_send_state.value
        self.metadata["composer_state"] = observation.composer_state.value
        self.metadata["composer_text_matches_expected"] = (
            observation.composer_text_matches_expected
        )
        self.metadata["last_message_visible"] = observation.last_message_visible

    def mark_business_activity(self):
        self.last_business_activity_at = monotonic()

    def block_auth(self, seconds: float):
        self.auth_blocked_until = monotonic() + max(0.0, seconds)

    @property
    def auth_cooldown_active(self):
        return monotonic() < self.auth_blocked_until

    def confirm_logged_in(self):
        self.auth_state = AuthState.LOGGED_IN
        self.auth_blocked_until = 0.0
        self.temporary_error_count = 0
        self.auth_probe_attempted = False
