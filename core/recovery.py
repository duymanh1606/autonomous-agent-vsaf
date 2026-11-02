import asyncio

from models.action import Action
from models.auth import AuthState


class RecoveryManager:
    def __init__(
        self, max_retries=3, reobserve_action="browser.snapshot",
        back_action="browser.back", screenshot_action="browser.screenshot",
        temporary_backoff_seconds=(2, 5, 15),
    ):
        self.max_retries = max_retries
        self.reobserve_action = reobserve_action
        self.back_action = back_action
        self.screenshot_action = screenshot_action
        self.temporary_backoff_seconds = tuple(temporary_backoff_seconds)

    async def recover(self, task, action, observation, context):
        if observation and observation.auth_state == AuthState.AUTH_REQUIRED:
            return Action(name="abort_task", args={
                "reason": "AUTH_REQUIRED: log in manually in Dedicated Chrome"
            })
        if observation and observation.auth_state == AuthState.AUTH_RATE_LIMITED:
            return Action(name="abort_task", args={
                "reason": "AUTH_RATE_LIMITED: authentication retry is prohibited"
            })
        if observation and observation.auth_state == AuthState.SECURITY_CHALLENGE:
            return Action(name="abort_task", args={
                "reason": "SECURITY_CHALLENGE: manual intervention required"
            })
        if observation and observation.auth_state == AuthState.TEMPORARY_ERROR:
            index = context.temporary_error_count
            if index >= len(self.temporary_backoff_seconds):
                return Action(name="abort_task", args={"reason": "Temporary error backoff limit reached"})
            context.temporary_error_count += 1
            await asyncio.sleep(self.temporary_backoff_seconds[index])
            return Action(name=self.reobserve_action, reason="Reobserve after temporary-error cooldown")
        if observation and observation.security_challenge:
            return Action(name="abort_task", args={"reason": "SECURITY_CHALLENGE: manual intervention required"})
        context.retry_count += 1
        if context.retry_count == 1:
            return Action(name=self.reobserve_action, reason="Reobserve after failed action")
        if context.retry_count == 2:
            return Action(name=self.back_action, reason="Return to a known navigation state")
        if context.retry_count < self.max_retries:
            return Action(name=self.screenshot_action, reason="Use visual fallback for unknown UI")
        return Action(name="abort_task", args={"reason": "Recovery limit reached"})
