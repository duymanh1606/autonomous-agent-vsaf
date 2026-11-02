"""PolicyEngine's hard guard between conversation_control mode and task origin.

No LLM, no browser - `allow()` is exercised directly with a minimal fake
context, matching the truth table from the human-takeover spec:

    origin=agent, mode=AUTO             -> allow
    origin=agent, mode=WAITING_HUMAN    -> block
    origin=agent, mode=HUMAN            -> block
    origin=human, mode=HUMAN            -> allow
    origin=human, mode=AUTO             -> block
    origin=human, mode=WAITING_HUMAN    -> block
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.policy import PolicyEngine
from models.action import Action
from models.auth import AuthState
from models.tasks import Task
from tests.support import make_database
from storage.conversation_control import ConversationControlRepository


SEND_ACTIONS = (
    "platform.send_message", "platform.retry_send",
    "platform.focus_message_input", "platform.attach_message_media",
)


def build_context():
    return SimpleNamespace(
        auth_enforced=False,
        auth_state=AuthState.LOGGED_IN,
        security_challenge=False,
        action_count=0,
        retry_count=0,
        task_runtime_seconds=0,
        current_screen="TIKTOK_CONVERSATION",
        metadata={},
    )


def build_task(conversation_id, origin):
    return Task(
        id="reply-task-1",
        account_id="acc_test",
        capability="send_message" if origin == "human" else "reply_to_message",
        platform="tiktok_web",
        payload={
            "conversation_id": conversation_id,
            "origin": origin,
            "confirm_send": True,
        },
    )


def build_action(name):
    return Action(name=name, args={"recipient": "customer"})


class PolicyConversationControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.conversation_control = ConversationControlRepository(self.database)
        self.database.conn.execute(
            "INSERT INTO conversations(account_id, platform_conversation_id) "
            "VALUES (%s, %s)",
            (self.database.account_id, "conv-1"),
        )
        self.database.conn.commit()
        self.policy = PolicyEngine(
            allowed_tools=SEND_ACTIONS,
            conversation_control=self.conversation_control,
        )

    async def test_agent_origin_allowed_only_while_auto(self):
        task = build_task("conv-1", origin="agent")
        for name in SEND_ACTIONS:
            self.assertTrue(
                await self.policy.allow(build_action(name), build_context(), task),
                f"{name} should be allowed for origin=agent while AUTO",
            )

    async def test_agent_origin_blocked_after_manual_takeover(self):
        await self.conversation_control.takeover("conv-1", "operator-1")
        task = build_task("conv-1", origin="agent")
        for name in SEND_ACTIONS:
            self.assertFalse(
                await self.policy.allow(build_action(name), build_context(), task),
                f"{name} should be blocked for origin=agent once HUMAN",
            )

    async def test_agent_origin_blocked_while_waiting_for_human(self):
        await self.conversation_control.wait_for_human("conv-1", "requires_human")
        task = build_task("conv-1", origin="agent")
        self.assertFalse(
            await self.policy.allow(
                build_action("platform.send_message"), build_context(), task
            )
        )

    async def test_human_origin_allowed_only_while_human(self):
        await self.conversation_control.takeover("conv-1", "operator-1")
        task = build_task("conv-1", origin="human")
        for name in SEND_ACTIONS:
            self.assertTrue(
                await self.policy.allow(build_action(name), build_context(), task),
                f"{name} should be allowed for origin=human while HUMAN",
            )

    async def test_human_origin_blocked_while_auto(self):
        task = build_task("conv-1", origin="human")
        self.assertFalse(
            await self.policy.allow(
                build_action("platform.send_message"), build_context(), task
            )
        )

    async def test_human_origin_blocked_while_waiting_for_human(self):
        await self.conversation_control.wait_for_human("conv-1", "requires_human")
        task = build_task("conv-1", origin="human")
        self.assertFalse(
            await self.policy.allow(
                build_action("platform.send_message"), build_context(), task
            )
        )

    async def test_case_3_task_queued_before_takeover_is_blocked_at_execution(self):
        """An agent send task built while AUTO must still be blocked if the
        conversation is taken over before the action actually executes -
        PolicyEngine reads the *live* mode, not a snapshot taken at task
        creation."""
        task = build_task("conv-1", origin="agent")
        action = build_action("platform.send_message")
        context = build_context()

        self.assertTrue(await self.policy.allow(action, context, task))

        await self.conversation_control.takeover("conv-1", "operator-1")

        self.assertFalse(await self.policy.allow(action, context, task))


if __name__ == "__main__":
    unittest.main()
