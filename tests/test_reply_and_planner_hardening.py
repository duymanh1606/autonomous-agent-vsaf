"""Ways the agent could act on a customer outside the one checked path.

* ReplyMessageSkill used to write a reply itself when a task arrived without
  one, prompting a text model with the raw text on screen - customer messages
  included - and sending the result with no ReplyValidator and no product
  data behind it. It now refuses, before it touches the UI.
* A capability with no skill and no deterministic route fell through to the
  planner's LLM, which is shown the whole observation (customer text again)
  before choosing a UI action. That fallback is now opt-in, and while it is
  off such a capability is refused when submitted rather than run.
* A second, queue-driven reply path fed by the observer's vision fallback,
  whose event ids never matched the ones the inbox scan settles by - so one
  customer message could be answered by both. Only the scan answers now.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main
from control.auth import TOKEN_HEADER
from control.events import EventBus
from control.server import ControlServer
from control.state import RuntimeStatus
from models.tasks import Task
from platforms.tiktok.messaging import ReplyMessageSkill
from platforms.tiktok_web.adapter import DETERMINISTIC_ROUTES


class EmptyReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_reply_is_refused_before_the_ui_is_touched(self):
        skill = ReplyMessageSkill()
        task = Task(
            account_id="acc_test", id="r-1", capability="reply_to_message",
            platform="tiktok_web",
            payload={"recipient": "khach", "confirm_send": True},
        )

        # `None` as the context: reading the screen at all would raise.
        result = await skill.execute(task, None)

        self.assertFalse(result.success)
        self.assertIn("never writes replies itself", result.error)

    def test_the_skill_no_longer_takes_a_text_model(self):
        with self.assertRaises(TypeError):
            ReplyMessageSkill(text_ai=object())


def _agent_with(*capability_lists):
    skills = [SimpleNamespace(capabilities=list(c)) for c in capability_lists]
    return SimpleNamespace(graph=SimpleNamespace(
        router=SimpleNamespace(skills=SimpleNamespace(skills=skills))
    ))


class Config(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class SubmittableCapabilityTests(unittest.TestCase):
    def test_skills_and_deterministic_routes_are_submittable(self):
        allowed = main._submittable_capabilities(
            _agent_with(["reply_to_message", "send_message"], ["messaging.scan_inbox"]),
            Config(),
        )
        self.assertIn("send_message", allowed)
        self.assertIn("messaging.scan_inbox", allowed)
        self.assertTrue(set(DETERMINISTIC_ROUTES) <= allowed)
        self.assertNotIn("anything_the_llm_would_have_to_improvise", allowed)

    def test_opting_into_the_llm_fallback_lifts_the_limit(self):
        self.assertIsNone(main._submittable_capabilities(
            _agent_with(["send_message"]), Config({"ai.planner.llm_fallback": True}),
        ))


class ControlSubmitTests(unittest.TestCase):
    def test_a_refused_capability_is_a_400_for_the_caller(self):
        async def refuse(**task):
            raise ValueError("unknown capability 'x'")

        status = RuntimeStatus(
            account_id="acc_test", runtime="langgraph",
            agent=SimpleNamespace(state=SimpleNamespace(
                running=True, paused=False, pause_reason="", current_task_id=None,
            )),
        )
        server = ControlServer(status, EventBus("acc_test"), task_submitter=refuse)
        client = TestClient(server.app, headers={TOKEN_HEADER: server.token})

        response = client.post("/control/tasks", json={"capability": "x", "payload": {}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown capability", response.json()["detail"])


class SingleReplyPathTests(unittest.TestCase):
    def test_the_runtime_answers_only_through_the_inbox_scan(self):
        from unittest import mock

        from load_config import load_config

        fake = SimpleNamespace(account_id="acc_test")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            runtime, _, provider, _, _ = main._build_message_runtime(
                load_config(), fake, fake, "tiktok_web", state=SimpleNamespace(),
            )

        # No second, queue-driven way to answer exists any more.
        for name in ("processor", "outbound_queue", "outbound_executor"):
            self.assertFalse(hasattr(runtime, name))
        self.assertIsNotNone(runtime.inbox_watcher)
        # The provider the scan answers with is still built.
        self.assertEqual(type(provider).__name__, "PipelineReplyProvider")


if __name__ == "__main__":
    unittest.main()
