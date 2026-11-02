import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic

from models.auth import AuthState
from models.message_delivery import MessageSendState, SendTaskState

logger = logging.getLogger(__name__)


MESSAGING_CAPABILITIES = frozenset({
    "send_message", "messaging.send",
    "reply_to_message", "messaging.reply",
    "send_message_batch", "messaging.batch",
})

MESSAGE_SCAN_CAPABILITIES = frozenset({
    "messaging.scan_inbox", "check_unread_messages",
    # The one-time historical inbox import (InboxBackfillSkill) needs the
    # same plain-open/list actions as an ongoing scan - it just walks every
    # conversation once instead of only the unread ones.
    "messaging.backfill_inbox",
})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    action: str
    capability: str
    account_id: str
    risk_level: str

    def __bool__(self):
        return self.allowed


class PolicyEngine:
    """Central gate applied to every action, including skill actions."""

    def __init__(
        self,
        allowed_tools,
        blocked_tools=(),
        max_actions_per_task=100,
        max_retries_per_action=3,
        max_task_runtime_seconds=300,
        interaction_limits=None,
        visual_target_confidence_threshold=0.85,
        send_repository=None,
        conversation_control=None,
    ):
        self.allowed_tools = frozenset(allowed_tools) | {"abort_task"}
        self.blocked_tools = frozenset(blocked_tools)
        self.max_actions = max_actions_per_task
        self.max_retries = max_retries_per_action
        self.max_runtime = max_task_runtime_seconds

        default_limits = {
            "platform.send_message": {
                "max": 5,
                "window_seconds": 3600,
            },
            "platform.reply_comment": {
                "max": 5,
                "window_seconds": 3600,
            },
            "platform.publish_video": {
                "max": 3,
                "window_seconds": 3600,
            },
        }

        self.interaction_limits = {
            name: dict(value)
            for name, value in default_limits.items()
        }

        for name, override in (interaction_limits or {}).items():
            if not isinstance(override, dict):
                continue

            if name in self.interaction_limits:
                self.interaction_limits[name].update(override)
            else:
                self.interaction_limits[name] = dict(override)


        self.interactions = defaultdict(deque)
        self.visual_target_confidence_threshold = float(visual_target_confidence_threshold)
        self.send_repository = send_repository
        # Whether the agent or a human currently owns each conversation.
        self.conversation_control = conversation_control

    async def allow(self, action, context, task=None) -> bool:
        normalized_name = action.name.casefold().replace("-", "_")
        if action.name != "browser.check_auth" and any(
            marker in normalized_name
            for marker in ("login", "log_in", "signin", "sign_in", "submit_auth")
        ):
            return False
        sensitive_keys = {
            "username", "password", "passcode", "otp", "verification_code",
            "recovery_code", "email", "phone",
        }
        if sensitive_keys.intersection(key.casefold() for key in action.args):
            return False
        if action.name not in self.allowed_tools or action.name in self.blocked_tools:
            return False
        if action.name == "abort_task":
            return True
        if action.name == "platform.clear_message_draft":
            # Clearing local composer state is the emergency brake for every
            # failed messaging mutation. It must remain available after a send
            # policy refusal or exhausted action budget, but only inside a DM
            # task on the conversation screen.
            return bool(
                task
                and task.capability in MESSAGING_CAPABILITIES
                and context.current_screen == "TIKTOK_CONVERSATION"
            )
        passive_auth_actions = {
            "browser.check_auth", "browser.snapshot", "browser.screenshot",
        }
        if context.auth_enforced and context.auth_state != AuthState.LOGGED_IN:
            if action.name not in passive_auth_actions:
                return False
        if context.security_challenge:
            return action.name in {"browser.snapshot", "browser.screenshot"}
        if context.action_count >= self.max_actions:
            return False
        if context.retry_count >= self.max_retries:
            return False
        if context.task_runtime_seconds >= self.max_runtime:
            return False
        if action.name == "platform.open_conversation":
            # An inbox scan uses the plain open action only for a durable
            # unanswered-ledger revisit: TikTok cleared that row's unread flag
            # when it was first opened. Restricting this action to send tasks
            # made every revisit fail policy before a click could occur.
            if task is None or task.capability not in (
                MESSAGING_CAPABILITIES | MESSAGE_SCAN_CAPABILITIES
            ):
                return False
        if action.name in {
            "platform.focus_message_input",
            "platform.attach_message_media", "platform.clear_message_draft",
            "platform.send_message",
            "platform.retry_send",
        }:
            if task is None or task.capability not in MESSAGING_CAPABILITIES:
                return False
        if action.name == "platform.list_conversations":
            # Read-only. The inbox scan reads the rows' previews to find
            # threads read on another device (InboxScanSkill._sync_read_
            # elsewhere); limiting this to send tasks silently disabled that.
            if task is None or task.capability not in (
                MESSAGING_CAPABILITIES | MESSAGE_SCAN_CAPABILITIES
            ):
                return False
        if self.conversation_control is not None and action.name in {
            "platform.focus_message_input", "platform.attach_message_media",
            "platform.send_message", "platform.retry_send",
        }:
            # A conversation owned by a human must never receive an
            # agent-generated action, and a human-submitted action must never
            # go out through a conversation the agent still owns - even when
            # the task was created before the mode changed. `mode()` is read
            # fresh here, at the moment the action actually executes.
            if task is None:
                return False
            conversation_id = str(task.payload.get("conversation_id") or "")
            origin = str(task.payload.get("origin") or "agent")
            mode = await self.conversation_control.mode(conversation_id)
            if origin == "agent" and mode != "AUTO":
                return False
            if origin == "human" and mode != "HUMAN":
                return False
        if action.name in {
            "platform.open_unread_conversation",
            "platform.list_unread_conversations",
            "platform.list_all_conversations",
            "platform.reload_page",
            "platform.detect_message_requests",
            "platform.open_message_requests",
            "platform.list_message_requests",
            "platform.open_message_request",
            "platform.accept_message_request",
            "platform.return_to_primary_inbox",
        }:
            if task is None or task.capability not in MESSAGE_SCAN_CAPABILITIES:
                return False
        if action.name == "browser.click_bbox":
            if context.auth_state != AuthState.LOGGED_IN or context.security_challenge:
                return False
            if set(action.args) != {"target_id"}:
                return False
            target = context.metadata.get("visual_targets", {}).get(action.args.get("target_id"))
            if not target or target.get("confidence", 0.0) < self.visual_target_confidence_threshold:
                return False
            target_type = target.get("type")
            if target_type not in {
                "inbox_button", "conversation", "conversation_item",
                "unread_conversation", "message_input", "send_button",
                "back_button", "popup_close",
            }:
                return False
            if target_type == "inbox_button":
                return bool(
                    task and task.capability in (MESSAGING_CAPABILITIES | {
                        "open_inbox", "check_unread_messages",
                        "messaging.scan_inbox",
                    })
                )
            if target_type in {
                "conversation", "conversation_item", "unread_conversation",
            }:
                return bool(
                    task and task.capability in (
                        MESSAGING_CAPABILITIES | MESSAGE_SCAN_CAPABILITIES
                    )
                    and context.current_screen == "TIKTOK_MESSAGES"
                )
            if target_type in {"message_input", "send_button"}:
                return bool(
                    task and task.capability in MESSAGING_CAPABILITIES
                    and task.payload.get("confirm_send", False)
                    and context.current_screen == "TIKTOK_CONVERSATION"
                )
            return context.auth_state == AuthState.LOGGED_IN
        if action.name in {
            "platform.attach_message_media", "platform.send_message",
            "platform.retry_send", "platform.reply_comment",
        }:
            if task is None or not task.payload.get("confirm_send", False):
                return False
        if action.name in {
            "platform.focus_message_input", "platform.attach_message_media",
            "platform.send_message", "platform.retry_send",
        } and not str(action.args.get("recipient") or "").strip():
            return False
        if self.send_repository is not None and action.name in {
            "platform.send_message", "platform.retry_send"
        } and (
            context.current_screen != "TIKTOK_CONVERSATION"
            or context.auth_state != AuthState.LOGGED_IN
        ):
            return False
        if action.name in {"platform.send_message", "platform.retry_send"}:
            if task is None:
                return False
            if self.send_repository is None:
                # Lightweight/in-memory adapters retain legacy policy checks.
                # Production wiring always supplies the task repository.
                pass
            else:
                # The signature identifies the reply itself (platform +
                # conversation + inbound message + reply text), so it cannot be
                # recomputed here. It must be the one this task bound before
                # composing, and the dedup row must still be claimed by it.
                signature = str(action.args.get("message_signature") or "")
                if not signature:
                    return False
                send_state = await self.send_repository.get_send_state(task.id)
                if not send_state or send_state.get("message_signature") != signature:
                    return False
                claim = await self._send_claim(signature)
                if claim is not None and (
                    claim.get("state") == SendTaskState.SENT.value
                    or str(claim.get("task_id") or "") != task.id
                ):
                    return False
                attempts = int(send_state.get("send_attempt_count") or 0)
                if action.name == "platform.send_message" and attempts != 0:
                    return False
                if action.name == "platform.retry_send" and not (
                    attempts == 1
                    and send_state.get("send_status") == MessageSendState.NOT_SENT.value
                    and context.metadata.get("message_send_state") == MessageSendState.NOT_SENT.value
                    and context.metadata.get("composer_text_matches_expected") is True
                ):
                    return False
        if action.name in {"platform.send_message", "platform.retry_send"} and action.args.get("visual_target_id"):
            target = context.metadata.get("visual_targets", {}).get(
                action.args["visual_target_id"]
            )
            if not target or target.get("type") != "message_input" or (
                target.get("confidence", 0.0) < self.visual_target_confidence_threshold
            ):
                return False
        if action.name == "platform.publish_video":
            if task is None or task.capability != "publish_video" or not task.payload.get("confirm_publish", False):
                return False
        if action.name == "browser.input_text":
            if task is None or task.capability not in MESSAGING_CAPABILITIES.union({
                "reply_to_comment",
            }) or not task.payload.get("confirm_send", False):
                return False
            visual_target_id = action.args.get("visual_target_id")
            if visual_target_id:
                target = context.metadata.get("visual_targets", {}).get(visual_target_id)
                if not target or target.get("type") != "message_input" or (
                    target.get("confidence", 0.0) < self.visual_target_confidence_threshold
                ):
                    return False
            if task.capability in MESSAGING_CAPABILITIES and not str(
                action.args.get("recipient") or ""
            ).strip():
                return False
        limit_name = (
            "platform.send_message"
            if action.name == "platform.retry_send" else action.name
        )
        limit = self.interaction_limits.get(limit_name)

        # The per-workspace send limit is a brake on the agent - an automated
        # sender - not on a person replying by hand. Counting operator sends
        # against it meant that once the agent had used the hour's budget,
        # every message a person sent failed too (reported: a row of "Gửi thất
        # bại", with the policy refusing the send and nothing saying why).
        if limit and not self._is_human_send(task):
            max_actions = limit.get("max")
            window_seconds = limit.get("window_seconds")

            if max_actions is None or window_seconds is None:
                return False

            self._prune(
                limit_name,
                float(window_seconds),
            )

            if len(self.interactions[limit_name]) >= int(max_actions):
                return False
        return True

    async def decide(self, action, context, task=None) -> PolicyDecision:
        """Structured audit result for the per-action policy gate."""
        allowed = await self.allow(action, context, task)
        if action.name in {
            "browser.input_text", "platform.attach_message_media",
            "platform.send_message", "platform.retry_send",
            "platform.accept_message_request",
        }:
            risk = "high"
        elif action.name.startswith("platform.open") or action.name == "browser.click_bbox":
            risk = "medium"
        else:
            risk = "low"
        reason = "approved"
        if not allowed:
            reason = "action_policy_condition_failed"
            try:
                reason += ": " + await self._denial_reason(action, context, task)
            except Exception:  # the diagnosis must never turn a refusal into a crash
                logger.warning("[policy] could not explain refusal of %s", action.name,
                               exc_info=True)
        return PolicyDecision(
            allowed=allowed,
            reason=reason,
            action=action.name,
            capability=str(getattr(task, "capability", "") or ""),
            account_id=str(getattr(task, "account_id", "") or ""),
            risk_level=risk,
        )

    async def _send_claim(self, signature):
        """The dedup row for this signature, when the repository provides one."""
        getter = getattr(self.send_repository, "get_send_task", None)
        if getter is None:
            return None
        return await getter(signature)

    async def reserve_send(self, action, task):
        if action.name not in {"platform.send_message", "platform.retry_send"}:
            return True
        if not self.send_repository:
            return True
        if task is None:
            return False
        return await self.send_repository.reserve_send_attempt(
            task.id,
            action.args.get("message_signature", ""),
            retry=action.name == "platform.retry_send",
        )

    def record(self, action, task=None):
        name = "platform.send_message" if action.name == "platform.retry_send" else action.name
        if name in self.interaction_limits and not self._is_human_send(task):
            self.interactions[name].append(monotonic())

    @staticmethod
    def _is_human_send(task) -> bool:
        return task is not None and str(task.payload.get("origin") or "") == "human"

    async def _denial_reason(self, action, context, task) -> str:
        """Which condition refused the action, for the log.

        `allow` answers yes/no from a long list of checks; the log line used to
        read only "action_policy_condition_failed", so an operator could not
        tell a spent rate limit from a conversation a person no longer holds.
        This names the common ones, in the order `allow` meets them.
        """
        if action.name not in self.allowed_tools or action.name in self.blocked_tools:
            return "tool_not_allowed"
        if context.auth_enforced and context.auth_state != AuthState.LOGGED_IN:
            return f"auth={getattr(context.auth_state, 'value', context.auth_state)}"
        if context.action_count >= self.max_actions:
            return f"action_budget_spent {context.action_count}/{self.max_actions}"
        if context.retry_count >= self.max_retries:
            return f"retry_budget_spent {context.retry_count}/{self.max_retries}"
        if task is not None and self.conversation_control is not None and action.name in {
            "platform.focus_message_input", "platform.attach_message_media",
            "platform.send_message", "platform.retry_send",
        } and task.capability in MESSAGING_CAPABILITIES:
            conversation_id = str(task.payload.get("conversation_id") or "")
            origin = str(task.payload.get("origin") or "agent")
            mode = await self.conversation_control.mode(conversation_id)
            if (origin == "agent" and mode != "AUTO") or (origin == "human" and mode != "HUMAN"):
                return f"conversation_mode={mode} origin={origin}"
        if action.name in {"platform.send_message", "platform.retry_send"}:
            if task is None or not task.payload.get("confirm_send", False):
                return "confirm_send_missing"
            if self.send_repository is not None and context.current_screen != "TIKTOK_CONVERSATION":
                return f"screen={context.current_screen}"
        name = "platform.send_message" if action.name == "platform.retry_send" else action.name
        limit = self.interaction_limits.get(name)
        if limit and not self._is_human_send(task):
            used = len(self.interactions[name])
            if limit.get("max") is not None and used >= int(limit["max"]):
                history = self.interactions[name]
                wait = int(float(limit.get("window_seconds") or 0) - (monotonic() - history[0])) + 1
                return (
                    f"rate_limited {name} {used}/{limit['max']} per "
                    f"{limit.get('window_seconds')}s, next slot in {max(wait, 0)}s"
                )
        if action.name in {"platform.send_message", "platform.retry_send"}:
            return "send_state_or_claim_mismatch"
        return "other"

    def _prune(self, action_name, window_seconds):
        cutoff = monotonic() - window_seconds
        history = self.interactions[action_name]
        while history and history[0] < cutoff:
            history.popleft()
