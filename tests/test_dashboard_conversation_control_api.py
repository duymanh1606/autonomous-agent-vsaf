"""ControlServer's conversation-control routes, and the task-submission
idempotency pattern the dashboard's manual-send flow relies on.

Credential-free: no Chrome, no LLM, a real temporary Postgres schema for the
repository-backed pieces (`tests/support.make_database`).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from control.auth import TOKEN_HEADER

from control.events import EventBus
from control.server import ControlServer
from control.state import RuntimeStatus
from core.policy import PolicyEngine
from models.action import Action
from models.auth import AuthState
from models.tasks import Task
from storage.conversation_control import ConversationControlRepository
from storage.repositories import TaskRepository
from tests.support import make_database


def register_conversation(database, conversation_id):
    database.conn.execute(
        "INSERT INTO conversations(account_id, platform_conversation_id) "
        "VALUES (%s, %s)",
        (database.account_id, conversation_id),
    )
    database.conn.commit()


async def submit_idempotent(database, repository, account_id, *, task_id,
                            capability, payload, priority=5, platform="tiktok_web"):
    """The exact pattern `main.py`'s control-server `submit` closure uses: a
    caller-supplied id makes submission a no-op on retry, since
    `TaskRepository.submit` has no ON CONFLICT clause of its own."""
    existing = database.conn.execute(
        "SELECT id FROM tasks WHERE account_id = %s AND id = %s",
        (account_id, task_id),
    ).fetchone()
    if existing is None:
        await repository.submit(Task(
            id=task_id, account_id=account_id, capability=capability,
            platform=platform, priority=priority, payload=payload,
        ))
    return task_id


class ControlServerConversationRoutesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = make_database(self)
        self.conversation_control = ConversationControlRepository(self.database)
        register_conversation(self.database, "conv-1")
        status = RuntimeStatus(
            account_id="acc_test", runtime="langgraph",
            agent=None, health_probe=lambda: {},
        )
        self.server = ControlServer(
            status, EventBus("acc_test"),
            conversation_control=self.conversation_control,
        )
        self.client = TestClient(
            self.server.app, headers={TOKEN_HEADER: self.server.token}
        )

    def test_case8_waiting_human_manual_takeover_becomes_human(self):
        self.database.conn.execute(
            "INSERT INTO conversation_control(account_id, conversation_id, mode, "
            "takeover_source) VALUES (%s, 'conv-1', 'WAITING_HUMAN', 'ESCALATION')",
            (self.database.account_id,),
        )
        self.database.conn.commit()

        response = self.client.post(
            "/control/conversations/conv-1/takeover",
            json={"operator_id": "operator-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "HUMAN")

    def test_the_takeover_log_line_carries_its_own_duration(self):
        """How long a takeover actually takes was unmeasurable: the only thing
        the log said was that one happened. `elapsed_ms` is the database work
        alone, and the supervisor logs the whole call, so the difference
        between the two is what the request spent waiting for the agent's
        event loop - the part that is worth knowing."""
        self.database.conn.execute(
            "INSERT INTO conversation_control(account_id, conversation_id, mode, "
            "takeover_source) VALUES (%s, 'conv-1', 'WAITING_HUMAN', 'ESCALATION')",
            (self.database.account_id,),
        )
        self.database.conn.commit()

        with self.assertLogs("control.server", level="INFO") as captured:
            response = self.client.post(
                "/control/conversations/conv-1/takeover",
                json={"operator_id": "operator-1"},
            )

        self.assertEqual(response.status_code, 200)
        line = next(
            message for message in captured.output
            if "conversation takeover" in message
        )
        self.assertIn("mode=HUMAN", line)
        elapsed = float(line.rsplit("elapsed_ms=", 1)[1])
        # A real duration, and not one that only looks real: two indexed
        # queries against a local Postgres are milliseconds, never seconds.
        self.assertGreater(elapsed, 0.0)
        self.assertLess(elapsed, 2000.0)

    def test_the_release_log_line_carries_its_duration_too(self):
        self.client.post(
            "/control/conversations/conv-1/takeover", json={"operator_id": "operator-1"},
        )

        with self.assertLogs("control.server", level="INFO") as captured:
            self.client.post("/control/conversations/conv-1/release")

        line = next(
            message for message in captured.output
            if "conversation released" in message
        )
        self.assertIn("mode=AUTO", line)
        self.assertGreater(float(line.rsplit("elapsed_ms=", 1)[1]), 0.0)

    def test_case9_human_release_becomes_auto(self):
        self.client.post(
            "/control/conversations/conv-1/takeover", json={"operator_id": "operator-1"},
        )

        response = self.client.post("/control/conversations/conv-1/release")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "AUTO")

    def test_takeover_needs_no_prior_escalation(self):
        response = self.client.post(
            "/control/conversations/conv-1/takeover", json={},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "HUMAN")

    def test_taking_over_a_claim_that_aged_out_still_succeeds(self):
        """The operator's click has to work on a conversation whose earlier
        claim quietly passed its TTL with nobody reading the row since - the
        ordinary shape of coming back to an old escalation. This used to 500
        and, worse, leave the conversation in AUTO for the agent to answer."""
        self.client.post(
            "/control/conversations/conv-1/takeover", json={"operator_id": "operator-1"},
        )
        self.database.conn.execute(
            "UPDATE conversation_control SET updated_at = "
            "CURRENT_TIMESTAMP - INTERVAL '10 hours' "
            "WHERE account_id = %s AND conversation_id = 'conv-1'",
            (self.database.account_id,),
        )
        self.database.conn.commit()

        response = self.client.post(
            "/control/conversations/conv-1/takeover", json={"operator_id": "operator-2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "HUMAN")
        self.assertEqual(response.json()["operator_id"], "operator-2")

    def test_missing_conversation_control_reports_unsupported(self):
        server = ControlServer(
            RuntimeStatus(account_id="acc_test", runtime="langgraph", agent=None,
                          health_probe=lambda: {}),
            EventBus("acc_test"),
        )
        client = TestClient(server.app, headers={TOKEN_HEADER: server.token})
        self.assertEqual(
            client.post("/control/conversations/conv-1/takeover", json={}).status_code, 501,
        )
        self.assertEqual(
            client.post("/control/conversations/conv-1/release").status_code, 501,
        )


class TaskSubmissionIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """Case 6: a retried manual-send POST with the same client_message_id
    must produce exactly one task/message, not two."""

    def setUp(self):
        self.database = make_database(self)
        self.repository = TaskRepository(self.database)

    async def test_case6_duplicate_task_id_creates_only_one_task(self):
        payload = {
            "conversation_id": "conv-1", "recipient": "customer",
            "response": "Dạ em kiểm tra giúp bạn ạ", "confirm_send": True,
            "origin": "human", "client_message_id": "client-msg-1",
        }
        first = await submit_idempotent(
            self.database, self.repository, self.database.account_id,
            task_id="human-conv-1-client-msg-1",
            capability="send_message", payload=payload,
        )
        second = await submit_idempotent(
            self.database, self.repository, self.database.account_id,
            task_id="human-conv-1-client-msg-1",
            capability="send_message", payload=payload,
        )

        self.assertEqual(first, second)
        rows = self.database.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE account_id = %s AND id = %s",
            (self.database.account_id, first),
        ).fetchone()
        self.assertEqual(rows["n"], 1)


class ManualSendPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """A verified send from the dashboard's manual-send task has no
    `outbound_messages` row (only the since-removed queue-driven reply path
    ever wrote that table), so `TaskRepository.complete`
    must write the OUTBOUND `conversation_messages` row directly from the
    task's own payload - otherwise a message that reached TikTok successfully
    would never show up in the dashboard."""

    async def test_completing_a_human_send_task_writes_conversation_messages(self):
        database = make_database(self)
        repository = TaskRepository(database)
        register_conversation(database, "conv-1")
        task = Task(
            id="human-conv-1-msg-1", account_id=database.account_id,
            capability="send_message", platform="tiktok_web",
            payload={
                "conversation_id": "conv-1", "recipient": "customer",
                "response": "Dạ em kiểm tra giúp bạn ạ", "confirm_send": True,
                "origin": "human", "client_message_id": "msg-1",
            },
        )
        await repository.submit(task)

        await repository.complete(task.id, True, None, {"status": "sent"}, task=task)

        rows = database.conn.execute(
            "SELECT direction, text, message_id FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = 'conv-1'",
            (database.account_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "OUTBOUND")
        self.assertEqual(rows[0]["text"], "Dạ em kiểm tra giúp bạn ạ")
        self.assertEqual(rows[0]["message_id"], "msg-1")

    async def test_completing_an_agent_task_does_not_use_the_human_fallback(self):
        """origin=agent tasks keep relying on their existing persistence
        mechanism (reply_provider.record_sent) - the fallback here must never
        double-write for them."""
        database = make_database(self)
        repository = TaskRepository(database)
        register_conversation(database, "conv-1")
        task = Task(
            id="agent-reply-1", account_id=database.account_id,
            capability="reply_to_message", platform="tiktok_web",
            payload={
                "conversation_id": "conv-1", "recipient": "customer",
                "response": "Dạ shop đây ạ", "confirm_send": True,
                "origin": "agent",
            },
        )
        await repository.submit(task)

        await repository.complete(task.id, True, None, {"status": "sent"}, task=task)

        rows = database.conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = 'conv-1'",
            (database.account_id,),
        ).fetchone()
        self.assertEqual(rows["n"], 0)

    async def test_a_failed_human_send_task_writes_nothing(self):
        database = make_database(self)
        repository = TaskRepository(database)
        register_conversation(database, "conv-1")
        task = Task(
            id="human-conv-1-msg-2", account_id=database.account_id,
            capability="send_message", platform="tiktok_web",
            payload={
                "conversation_id": "conv-1", "recipient": "customer",
                "response": "should not be written", "confirm_send": True,
                "origin": "human", "client_message_id": "msg-2",
            },
        )
        await repository.submit(task)

        await repository.complete(task.id, False, "send_unverified", {}, task=task)

        rows = database.conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = 'conv-1'",
            (database.account_id,),
        ).fetchone()
        self.assertEqual(rows["n"], 0)


class HumanSendPassesPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Case 5 (partial, no browser/TikTok in unit tests): a task built with
    capability=send_message, origin=human against a HUMAN-mode conversation
    passes PolicyEngine.allow() for platform.send_message - proving the
    dashboard's task reaches the same gate the agent's own sends do."""

    async def test_human_send_task_is_allowed_once_taken_over(self):
        database = make_database(self)
        conversation_control = ConversationControlRepository(database)
        register_conversation(database, "conv-1")
        await conversation_control.takeover("conv-1", "operator-1")

        policy = PolicyEngine(
            allowed_tools=("platform.send_message",),
            conversation_control=conversation_control,
        )
        task = Task(
            id="human-conv-1-msg-1", account_id=database.account_id,
            capability="send_message", platform="tiktok_web",
            payload={
                "conversation_id": "conv-1", "origin": "human",
                "confirm_send": True,
            },
        )
        context = type("Context", (), {
            "auth_enforced": False, "auth_state": AuthState.LOGGED_IN,
            "security_challenge": False, "action_count": 0, "retry_count": 0,
            "task_runtime_seconds": 0, "current_screen": "TIKTOK_CONVERSATION",
            "metadata": {},
        })()
        action = Action(name="platform.send_message", args={"recipient": "customer"})

        self.assertTrue(await policy.allow(action, context, task))


if __name__ == "__main__":
    unittest.main()
