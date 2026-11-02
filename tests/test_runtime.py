import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai.client import GeminiVisionClient
from core.graph import AgentLoop
from tests.support import (
    TemporaryPostgresSchema, make_database, requires_postgres,
)
from load_config import load_env_file
from llm.gemini_flash_lite import GeminiFlashLiteProvider
from core.memory import Memory
from core.llm_planner import LlmPlanner
from core.policy import PolicyEngine
from core.state import AgentState
from models.action import Action
from models.auth import AuthState
from models.message_event import (
    MessageDirection,
    MessageEvent,
    MessageEventStatus,
    MessageEventType,
)
from models.observation import Observation
from models.tasks import Task, TaskClaimState
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.adapter import TikTokWebAdapter
from storage.repositories import TaskRepository
from tools.registry import ToolRegistry


class SequenceObserver:
    def __init__(self, observations):
        self.observations = iter(observations)

    async def observe(self):
        return next(self.observations)


class FailingOnceObserver:
    def __init__(self, observations):
        self.observations = iter(observations)

    async def observe(self):
        value = next(self.observations)
        if isinstance(value, Exception):
            raise value
        return value


class NoopRouter:
    async def route(self, task):
        return None


class StaticPlanner:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    async def plan(self, task, observation, memory):
        self.calls += 1
        return self.action


class FakeAI:
    def __init__(self, response):
        self.response = response
        self.json_mode = None

    async def generate(self, prompt, temperature=0.2, json_mode=False):
        self.json_mode = json_mode
        return self.response


class GeminiVisionClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_png_inline_and_requests_json(self):
        captured = {}

        async def handler(request):
            captured["request"] = request
            return httpx.Response(
                200,
                json={
                    "candidates": [{
                        "content": {"parts": [{"text": '{"screen":"feed"}'}]}
                    }]
                },
            )

        client = GeminiVisionClient(
            "https://generativelanguage.googleapis.com/v1beta",
            "test-key",
            transport=httpx.MockTransport(handler),
        )
        result = await client.generate("Analyze", images=["cG5n"])
        request = captured["request"]
        payload = json.loads(request.content)

        self.assertEqual(result, '{"screen":"feed"}')
        self.assertTrue(str(request.url).endswith(
            "/models/gemini-3.1-flash-lite:generateContent"
        ))
        self.assertEqual(request.headers["x-goog-api-key"], "test-key")
        self.assertEqual(
            payload["contents"][0]["parts"][1]["inlineData"],
            {"mimeType": "image/png", "data": "cG5n"},
        )
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"], "application/json"
        )
        self.assertNotIn("responseJsonSchema", payload["generationConfig"])

    async def test_forwards_response_schema_for_constrained_decoding(self):
        captured = {}

        async def handler(request):
            captured["request"] = request
            return httpx.Response(
                200,
                json={
                    "candidates": [{
                        "content": {"parts": [{"text": '{"screen":"feed"}'}]}
                    }]
                },
            )

        client = GeminiVisionClient(
            "https://generativelanguage.googleapis.com/v1beta",
            "test-key",
            transport=httpx.MockTransport(handler),
        )
        schema = {"type": "object", "properties": {"screen": {"type": "string"}}}
        await client.generate("Analyze", images=["cG5n"], response_schema=schema)
        payload = json.loads(captured["request"].content)

        self.assertEqual(payload["generationConfig"]["responseJsonSchema"], schema)

    async def test_requires_api_key_and_image(self):
        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
            await GeminiVisionClient("https://example.test", "").generate(
                "Analyze", images=["cG5n"]
            )
        with self.assertRaisesRegex(ValueError, "at least one image"):
            await GeminiVisionClient("https://example.test", "key").generate("Analyze")


class GeminiTextClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_planner_interface_uses_flash_lite(self):
        captured = {}

        async def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "ready"}]}}]
            })

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        provider = GeminiFlashLiteProvider(
            api_key="test-key", client=http_client,
            model="gemini-3.1-flash-lite",
        )

        result = await provider.generate("Plan one action", json_mode=True)

        self.assertEqual(result, "ready")
        self.assertTrue(str(captured["request"].url).endswith(
            "/models/gemini-3.1-flash-lite:generateContent"
        ))
        payload = json.loads(captured["request"].content)
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"], "application/json"
        )


class MessageEventTests(unittest.TestCase):
    def test_inbound_event_has_stable_dedup_key_and_requires_reply(self):
        values = {
            "event_type": MessageEventType.RECEIVED,
            "platform": " TikTok_Web ",
            "conversation_id": "@TestUser",
            "source_message_id": "message-123",
            "sender": "TestUser",
            "text": "Hello",
            "direction": MessageDirection.INBOUND,
            "status": MessageEventStatus.NEEDS_REPLY,
        }
        first = MessageEvent(**values)
        second = MessageEvent(**values)

        self.assertEqual(first.platform, "tiktok_web")
        self.assertEqual(first.dedup_key, second.dedup_key)
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(first.requires_reply)
        self.assertIn("dedup_key", first.model_dump(mode="json"))

    def test_received_event_rejects_outbound_direction(self):
        with self.assertRaisesRegex(ValueError, "RECEIVED"):
            MessageEvent(
                event_type=MessageEventType.RECEIVED,
                platform="tiktok_web",
                conversation_id="test",
                customer_id="customer",
                message_id="m1",
                message="hello",
                timestamp=datetime.now(timezone.utc),
                direction=MessageDirection.OUTBOUND,
            )

    def test_media_only_inbound_event_is_valid_but_truly_empty_is_rejected(self):
        event = MessageEvent(
            platform="tiktok_web", conversation_id="test", customer_id="customer",
            message_id="media-1", message="", metadata={
                "media": [{"kind": "image", "src": "blob:customer-image"}],
            },
            timestamp=datetime.now(timezone.utc), direction=MessageDirection.INBOUND,
        )
        self.assertEqual(event.message, "")
        self.assertEqual(event.metadata["media"][0]["kind"], "image")
        with self.assertRaisesRegex(ValueError, "text or inbound media"):
            MessageEvent(
                platform="tiktok_web", conversation_id="test", customer_id="customer",
                message_id="empty-1", message="", timestamp=datetime.now(timezone.utc),
                direction=MessageDirection.INBOUND,
            )

    def test_sent_event_rejects_inbound_direction(self):
        with self.assertRaisesRegex(ValueError, "SENT"):
            MessageEvent(
                event_type=MessageEventType.SENT,
                platform="tiktok_web",
                conversation_id="test",
                customer_id="customer",
                message_id="m1",
                message="hello",
                timestamp=datetime.now(timezone.utc),
                direction=MessageDirection.INBOUND,
            )


class NoopRecovery:
    async def recover(self, *args):
        return Action(name="abort_task", args={"reason": "failed"})


class NoopScheduler:
    async def next_task(self):
        return None


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_cleanup_remains_available_after_action_budget_is_exhausted(self):
        policy = PolicyEngine(
            ["platform.clear_message_draft"], max_actions_per_task=1
        )
        state = AgentState(
            auth_enforced=True, auth_state=AuthState.UNKNOWN,
            current_screen=TikTokScreen.CONVERSATION, action_count=1,
        )
        state.begin_task("cleanup")
        state.current_screen = TikTokScreen.CONVERSATION
        state.action_count = 1
        task = Task(
            account_id="acc_test", id="cleanup", capability="reply_to_message",
            platform="tiktok_web",
        )

        self.assertTrue(await policy.allow(
            Action(name="platform.clear_message_draft"), state, task
        ))

    async def test_policy_decision_contains_auditable_action_scope_and_risk(self):
        policy = PolicyEngine(["platform.attach_message_media"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("media")
        task = Task(
            account_id="acc_test", id="media", capability="reply_to_message",
            platform="tiktok_web", payload={"confirm_send": True},
        )

        decision = await policy.decide(
            Action(
                name="platform.attach_message_media",
                args={"recipient": "customer"},
            ), state, task
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "platform.attach_message_media")
        self.assertEqual(decision.capability, "reply_to_message")
        self.assertEqual(decision.account_id, "acc_test")
        self.assertEqual(decision.risk_level, "high")

    async def test_send_requires_explicit_confirmation(self):
        policy = PolicyEngine(["platform.send_message"])
        state = AgentState()
        state.begin_task("one")
        task = Task(account_id="acc_test", id="one", capability="reply_to_message", platform="tiktok_web")
        action = Action(
            name="platform.send_message", args={"recipient": "customer"}
        )
        self.assertFalse(await policy.allow(action, state, task))
        task.payload["confirm_send"] = True
        self.assertTrue(await policy.allow(action, state, task))

    async def test_send_message_capability_may_type_only_after_confirmation(self):
        policy = PolicyEngine(["browser.input_text"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("send")
        task = Task(
            account_id="acc_test",
            id="send", capability="send_message", platform="tiktok_web",
            payload={"recipient": "Test User", "message": "Hello"},
        )
        action = Action(
            name="browser.input_text",
            args={"text": "Hello", "recipient": "Test User"},
        )
        self.assertFalse(await policy.allow(action, state, task))
        task.payload["confirm_send"] = True
        self.assertTrue(await policy.allow(action, state, task))

    async def test_media_send_requires_messaging_capability_and_confirmation(self):
        policy = PolicyEngine(["platform.attach_message_media"])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("media")
        action = Action(
            name="platform.attach_message_media", args={"recipient": "customer"}
        )
        task = Task(
            account_id="acc_test", id="media", capability="reply_to_message",
            platform="tiktok_web",
        )
        self.assertFalse(await policy.allow(action, state, task))
        task.payload["confirm_send"] = True
        self.assertTrue(await policy.allow(action, state, task))
        task.capability = "search_content"
        self.assertFalse(await policy.allow(action, state, task))

    async def test_non_messaging_task_cannot_use_send_tools(self):
        policy = PolicyEngine([
            "platform.open_conversation", "platform.focus_message_input",
            "platform.send_message",
        ])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("search")
        task = Task(
            account_id="acc_test",
            id="search", capability="search_content", platform="tiktok_web",
            payload={"confirm_send": True},
        )
        for name in (
            "platform.open_conversation", "platform.focus_message_input",
            "platform.send_message",
        ):
            self.assertFalse(await policy.allow(Action(name=name), state, task))

    async def test_inbox_scan_may_reopen_ledger_conversation_but_not_compose(self):
        policy = PolicyEngine([
            "platform.open_conversation", "platform.focus_message_input",
            "platform.send_message",
        ])
        state = AgentState(auth_enforced=True, auth_state=AuthState.LOGGED_IN)
        state.begin_task("scan")
        task = Task(
            account_id="acc_test", id="scan", capability="messaging.scan_inbox",
            platform="tiktok_web",
        )

        self.assertTrue(await policy.allow(
            Action(name="platform.open_conversation", args={"target": "customer"}),
            state, task,
        ))
        self.assertFalse(await policy.allow(
            Action(name="platform.focus_message_input"), state, task,
        ))
        self.assertFalse(await policy.allow(
            Action(name="platform.send_message"), state, task,
        ))

    async def test_security_challenge_stops_mutations(self):
        policy = PolicyEngine(["browser.input_text"])
        state = AgentState(security_challenge=True)
        self.assertFalse(await policy.allow(Action(name="browser.input_text"), state))

    async def test_interaction_rate_limit_spans_tasks(self):
        policy = PolicyEngine(
            ["platform.send_message"],
            interaction_limits={"platform.send_message": {"max": 1, "window_seconds": 3600}},
        )
        state = AgentState()
        state.begin_task("one")
        task = Task(
            account_id="acc_test",
            id="one", capability="reply_to_message", platform="tiktok_web",
            payload={"confirm_send": True},
        )
        action = Action(
            name="platform.send_message", args={"recipient": "customer"}
        )
        self.assertTrue(await policy.allow(action, state, task))
        policy.record(action)
        self.assertFalse(await policy.allow(action, state, task))


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_output_is_json_and_runtime_allow_listed(self):
        ai = FakeAI('{"name":"browser.back","args":{},"reason":"recover","confidence":0.9}')
        tools = ToolRegistry()
        tools.register("browser.back", lambda: None)

        class UnknownPlatform:
            def next_action(self, task, observation):
                return None

        planner = LlmPlanner(ai, tools, UnknownPlatform(), available_actions=("browser.back",))
        action = await planner.plan(
            Task(account_id="acc_test", id="p", capability="unknown", platform="tiktok_web"),
            Observation(screen="UNKNOWN", unknown=True), Memory(),
        )
        self.assertEqual(action.name, "browser.back")
        self.assertTrue(ai.json_mode)

    async def test_llm_cannot_invent_tool(self):
        ai = FakeAI('{"name":"system.shell","args":{},"confidence":1}')

        class UnknownPlatform:
            def next_action(self, task, observation):
                return None

        planner = LlmPlanner(ai, ToolRegistry(), UnknownPlatform(), available_actions=())
        with self.assertRaises(ValueError):
            await planner.plan(
                Task(account_id="acc_test", id="p", capability="unknown", platform="tiktok_web"),
                Observation(screen="UNKNOWN", unknown=True), Memory(),
            )


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_checkpoint_resumes_after_interrupted_node(self):
        schema = TemporaryPostgresSchema()
        self.addCleanup(schema.cleanup)
        before = Observation(screen=TikTokScreen.VIDEO_FEED, confidence=1, auth_state=AuthState.LOGGED_IN)
        after = Observation(screen=TikTokScreen.INBOX, confidence=1, auth_state=AuthState.LOGGED_IN)
        observer = FailingOnceObserver([RuntimeError("browser unavailable"), before, after])
        tools = ToolRegistry()

        async def tap_inbox():
            return {"success": True}

        tools.register("platform.open_inbox", tap_inbox)
        loop = AgentLoop(
            NoopScheduler(), observer, NoopRouter(),
            StaticPlanner(Action(name="platform.open_inbox")),
            PolicyEngine(tools.names), NoopRecovery(), tools,
            TikTokWebAdapter(None, observer), Memory(), AgentState(),
            checkpoint_dsn=schema.dsn,
            account="acc_resume",
        )
        task = Task(account_id="acc_resume", id="resume", capability="open_inbox", platform="tiktok_web")
        with self.assertRaisesRegex(RuntimeError, "browser unavailable"):
            await loop.run_task(task)
        result = await loop.run_task(task)
        self.assertTrue(result.success)
        # The checkpoint really lives in PostgreSQL, under a thread namespaced
        # by account so two accounts can never share one checkpoint thread.
        import psycopg
        with psycopg.connect(schema.dsn, row_factory=psycopg.rows.dict_row) as check:
            threads = check.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall()
        self.assertEqual([row["thread_id"] for row in threads], ["acc_resume:resume"])
        await loop.close()

    async def test_action_is_observed_and_screen_verified(self):
        before = Observation(screen=TikTokScreen.VIDEO_FEED, confidence=1, auth_state=AuthState.LOGGED_IN)
        after = Observation(screen=TikTokScreen.INBOX, confidence=1, auth_state=AuthState.LOGGED_IN)
        observer = SequenceObserver([before, after])
        tools = ToolRegistry()

        async def tap_inbox():
            return {"success": True}

        tools.register("platform.open_inbox", tap_inbox)
        platform = TikTokWebAdapter(None, observer)
        task = Task(account_id="acc_test", id="t1", capability="open_inbox", platform="tiktok_web")
        planner = StaticPlanner(Action(name="platform.open_inbox"))
        state = AgentState()
        loop = AgentLoop(
            NoopScheduler(), observer, NoopRouter(), planner,
            PolicyEngine(tools.names), NoopRecovery(), tools, platform, Memory(), state,
        )
        result = await loop.run_task(task)
        self.assertTrue(result.success)
        self.assertEqual(state.current_screen, TikTokScreen.INBOX)
        self.assertEqual(planner.calls, 1)

    async def test_tool_success_without_expected_ui_is_failure(self):
        before = Observation(screen=TikTokScreen.VIDEO_FEED, confidence=1, auth_state=AuthState.LOGGED_IN)
        unchanged = Observation(screen=TikTokScreen.VIDEO_FEED, confidence=1, auth_state=AuthState.LOGGED_IN)
        observer = SequenceObserver([before, unchanged])
        tools = ToolRegistry()

        async def tap_inbox():
            return {"success": True}

        tools.register("platform.open_inbox", tap_inbox)
        platform = TikTokWebAdapter(None, observer)
        loop = AgentLoop(
            NoopScheduler(), observer, NoopRouter(), StaticPlanner(Action(name="platform.open_inbox")),
            PolicyEngine(tools.names), NoopRecovery(), tools, platform, Memory(), AgentState(),
        )
        result = await loop.run_task(Task(account_id="acc_test", id="t2", capability="open_inbox", platform="tiktok_web"))
        self.assertFalse(result.success)


@requires_postgres
class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def second_repository(self, database, owner_id):
        from storage.database import Database

        other_db = Database(
            database.dsn, account_id=database.account_id, schema_path=None
        )
        self.addCleanup(other_db.close)
        return TaskRepository(other_db, owner_id=owner_id, task_lease_seconds=30)

    async def test_two_workers_cannot_run_the_same_capability_concurrently(self):
        database = make_database(self)
        first = TaskRepository(
            database, owner_id="worker-a", task_lease_seconds=30
        )
        second = self.second_repository(database, "worker-b")
        for suffix in ("a", "b"):
            await first.submit(Task(
                account_id="acc_test", id=f"scan-{suffix}",
                capability="messaging.scan_inbox", platform="tiktok_web",
            ))

        claims = await __import__("asyncio").gather(
            first.claim_next_task(), second.claim_next_task()
        )

        self.assertEqual(sum(bool(claim and claim.granted) for claim in claims), 1)
        self.assertIn(TaskClaimState.LOCKED, {
            claim.state for claim in claims if claim is not None
        })

    async def test_expired_task_lease_is_recovered_explicitly(self):
        database = make_database(self)
        first = TaskRepository(database, owner_id="worker-a", task_lease_seconds=30)
        second = self.second_repository(database, "worker-b")
        task = Task(
            account_id="acc_test", id="expired-scan",
            capability="messaging.scan_inbox", platform="tiktok_web",
        )
        await first.submit(task)
        self.assertEqual((await first.claim_task(task.id)).state, TaskClaimState.GRANTED)
        database.conn.execute(
            "UPDATE tasks SET lease_expires_at = %s WHERE account_id = %s AND id = %s",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
             "acc_test", task.id),
        )
        database.conn.commit()

        recovered = await second.claim_task(task.id)

        self.assertEqual(recovered.state, TaskClaimState.EXPIRED)
        self.assertTrue(recovered.granted)
        self.assertEqual(recovered.attempt_count, 2)

    async def test_running_tasks_are_requeued_for_checkpoint_resume(self):
        repo = TaskRepository(make_database(self))
        task = Task(account_id="acc_test", id="recover", capability="open_inbox", platform="tiktok_web")
        await repo.submit(task)
        await repo.get_next_task()
        self.assertEqual(repo.requeue_interrupted_tasks(), 1)
        recovered = await repo.get_next_task()
        self.assertEqual(recovered.id, task.id)

    async def test_task_claim_is_persisted(self):
        db = make_database(self)
        repo = TaskRepository(db)
        task = Task(account_id="acc_test", id="db1", capability="open_inbox", platform="tiktok_web")
        await repo.submit(task)
        claimed = await repo.get_next_task()
        self.assertEqual(claimed.id, task.id)
        self.assertIsNone(await repo.get_next_task())
        await repo.complete(
            task.id, False, "AUTH_REQUIRED: manual login",
            {"auth_state": "AUTH_REQUIRED"},
        )
        row = db.conn.execute(
            "SELECT status, error, result FROM tasks "
            "WHERE account_id = %s AND id = %s", ("acc_test", task.id)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("AUTH_REQUIRED", row["error"])
        self.assertIn("AUTH_REQUIRED", row["result"])


class EnvFileTests(unittest.TestCase):
    """`.env` is the documented home for API keys, so its loader is pinned."""

    def write_env(self, body):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def clear(self, *names):
        for name in names:
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)

    def test_loads_keys_and_ignores_comments_and_blank_lines(self):
        self.clear("AGENT_TEST_KEY", "AGENT_TEST_OTHER")
        path = self.write_env(
            "# a comment\n\nAGENT_TEST_KEY=abc123\nexport AGENT_TEST_OTHER=\"quoted\"\n"
        )
        applied = load_env_file(path)
        self.assertEqual(sorted(applied), ["AGENT_TEST_KEY", "AGENT_TEST_OTHER"])
        self.assertEqual(os.environ["AGENT_TEST_KEY"], "abc123")
        self.assertEqual(os.environ["AGENT_TEST_OTHER"], "quoted")

    def test_an_exported_variable_outranks_the_file(self):
        self.clear("AGENT_TEST_KEY")
        os.environ["AGENT_TEST_KEY"] = "from-environment"
        path = self.write_env("AGENT_TEST_KEY=from-file\n")
        self.assertEqual(load_env_file(path), [])
        self.assertEqual(os.environ["AGENT_TEST_KEY"], "from-environment")

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(load_env_file(Path("/nonexistent/.env")), [])

    def test_an_empty_value_is_not_applied(self):
        self.clear("AGENT_TEST_EMPTY")
        path = self.write_env("AGENT_TEST_EMPTY=\n")
        self.assertEqual(load_env_file(path), [])
        self.assertNotIn("AGENT_TEST_EMPTY", os.environ)


if __name__ == "__main__":
    unittest.main()


class IdleLoopWakeupTests(unittest.IsolatedAsyncioTestCase):
    """A manual reply must not sit through the idle poll tick.

    The dashboard's send is a queue insert; before this, the loop only noticed
    it on its next poll, so an operator watching the browser paid up to
    `idle_seconds` of pure waiting on every message.
    """

    def _loop(self, scheduler, idle_seconds):
        return AgentLoop(
            scheduler, SequenceObserver([]), NoopRouter(), StaticPlanner(None),
            PolicyEngine([]), NoopRecovery(), ToolRegistry(), None, Memory(),
            AgentState(), idle_seconds=idle_seconds,
        )

    async def test_submitting_a_task_ends_the_idle_wait_immediately(self):
        loop = self._loop(NoopScheduler(), idle_seconds=30)
        started = asyncio.get_running_loop().time()
        waiter = asyncio.ensure_future(loop._wait_for_task())
        await asyncio.sleep(0)
        loop.notify_task_submitted()
        await asyncio.wait_for(waiter, 1)
        self.assertLess(asyncio.get_running_loop().time() - started, 1)

    async def test_idle_wait_still_times_out_with_nothing_submitted(self):
        loop = self._loop(NoopScheduler(), idle_seconds=0.01)
        await asyncio.wait_for(loop._wait_for_task(), 1)

    async def test_a_submit_during_the_claim_is_not_lost(self):
        # The event is cleared before claiming, so a task arriving while
        # next_task() is already running leaves it set and the wait returns
        # at once rather than swallowing the wakeup.
        loop = self._loop(None, idle_seconds=30)

        class SubmitsWhileClaiming:
            async def next_task(self):
                loop.notify_task_submitted()
                return None

        loop.scheduler = SubmitsWhileClaiming()
        loop._task_submitted.clear()
        self.assertFalse(await loop.run_once())
        await asyncio.wait_for(loop._wait_for_task(), 1)
