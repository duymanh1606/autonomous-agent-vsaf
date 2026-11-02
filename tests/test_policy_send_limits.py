"""The agent's hourly send limit, and why a send was refused.

Reported with a log: two messages an operator sent from the dashboard were
typed into TikTok and then refused at the send step with only
"action_policy_condition_failed". Focusing the composer - which checks the
same conversation ownership - had passed a second earlier, so the takeover
was not it; the send-only checks were. The one that fits a busy shop is the
workspace's send limit (policy.interaction_limits, 15 per hour), which
counted agent replies and operator messages alike: once the agent had used
the hour, nobody could send. Now a person's messages neither count against
it nor are held by it, and a refusal says which condition failed.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.policy import PolicyEngine
from core.state import AgentState
from models.action import Action
from models.tasks import Task


def _policy(**extra):
    return PolicyEngine(
        ["platform.send_message"],
        interaction_limits={"platform.send_message": {"max": 2, "window_seconds": 3600}},
        **extra,
    )


def _task(task_id, origin, conversation="conv-1"):
    return Task(
        account_id="acc_test", id=task_id,
        capability="send_message" if origin == "human" else "reply_to_message",
        platform="tiktok_web",
        payload={"confirm_send": True, "origin": origin, "conversation_id": conversation},
    )


SEND = Action(name="platform.send_message", args={"recipient": "khach"})


class SendLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = AgentState()
        self.state.begin_task("t")

    async def test_an_operator_can_still_send_after_the_agent_used_the_hour(self):
        policy = _policy()
        agent = _task("a", "agent")
        for _ in range(2):
            self.assertTrue(await policy.allow(SEND, self.state, agent))
            policy.record(SEND, agent)
        self.assertFalse(await policy.allow(SEND, self.state, agent))

        human = _task("human-1", "human")
        self.assertTrue(await policy.allow(SEND, self.state, human))

    async def test_operator_sends_do_not_use_the_agents_budget(self):
        policy = _policy()
        human = _task("human-1", "human")
        for _ in range(5):
            policy.record(SEND, human)
        self.assertTrue(await policy.allow(SEND, self.state, _task("a", "agent")))

    async def test_a_refusal_says_the_limit_was_reached(self):
        policy = _policy()
        agent = _task("a", "agent")
        policy.record(SEND, agent)
        policy.record(SEND, agent)
        decision = await policy.decide(SEND, self.state, agent)
        self.assertFalse(decision.allowed)
        self.assertIn("rate_limited platform.send_message 2/2 per 3600s", decision.reason)

    async def test_a_refusal_says_the_conversation_is_not_the_operators(self):
        control = SimpleNamespace(mode=lambda cid: _async("AUTO"))
        policy = _policy(conversation_control=control)
        decision = await policy.decide(SEND, self.state, _task("human-1", "human"))
        self.assertFalse(decision.allowed)
        self.assertIn("conversation_mode=AUTO origin=human", decision.reason)


async def _async(value):
    return value


if __name__ == "__main__":
    unittest.main()
