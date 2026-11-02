"""Tests for the dashboard control plane and the pieces it depends on.

Credential-free: no Chrome, no network, no LLM. Everything here is either pure
logic or a temporary PostgreSQL schema.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from control.events import EventBus
from agent.followup_policy import FollowUpPolicy
from agent.scenario_policy import ScenarioPolicy
from control.settings import apply_runtime_settings
from control.state import RuntimeStatus
from core.schedule import OperatingHours
from dashboard_api.db_reader import AgentDatabaseReader, ReadOnlyViolation
from dashboard_api.notifications import NotificationStore
from dashboard_api.orchestrator import Orchestrator
from load_config import load_config
from models.outbound_message import Attachment, OutboundMessage
from platforms.tiktok.inbox_watcher import InboxScanSkill
from storage.database import Database
from tests.support import (
    SCHEMA_PATH, TemporaryPostgresSchema, requires_postgres, unscoped_connection,
)
from storage.product_repository import ProductRepository
from tools.business import ServiceMethodTool

BASE_DIR = Path(__file__).resolve().parents[1]


class OperatingHoursTests(unittest.TestCase):
    """The gate decides whether new work may START, never interrupts work."""

    @staticmethod
    def _at(hour, minute=0):
        return datetime(2026, 8, 19, hour, minute)   # a Wednesday

    def test_disabled_is_always_open(self):
        self.assertTrue(OperatingHours(enabled=False).is_open(self._at(3)))

    def test_inside_and_outside_a_normal_window(self):
        hours = OperatingHours(enabled=True, start="09:00", end="18:00")
        self.assertTrue(hours.is_open(self._at(10)))
        self.assertFalse(hours.is_open(self._at(3)))
        self.assertFalse(hours.is_open(self._at(18, 30)))

    def test_a_window_that_wraps_midnight_is_open_on_both_sides(self):
        hours = OperatingHours(enabled=True, start="22:00", end="06:00")
        self.assertTrue(hours.is_open(self._at(23)))
        self.assertTrue(hours.is_open(self._at(3)))
        self.assertFalse(hours.is_open(self._at(12)))

    def test_days_restrict_the_window(self):
        weekdays = OperatingHours(enabled=True, days=["mon", "tue", "wed"])
        weekend = OperatingHours(enabled=True, days=["sat", "sun"])
        self.assertTrue(weekdays.is_open(self._at(10)))
        self.assertFalse(weekend.is_open(self._at(10)))


@requires_postgres
class DatabaseReaderTests(unittest.TestCase):
    """The dashboard must never be able to write to an agent's database."""

    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.addCleanup(self.schema.cleanup)
        self.database = Database(
            self.schema.dsn, SCHEMA_PATH, account_id="acc_test",
        )
        self.addCleanup(self.database.close)
        self.database.conn.execute(
            "INSERT INTO tasks("
            "id, account_id, capability, platform, priority, payload, status) "
            "VALUES ('t1', 'acc_test', 'open_inbox', 'tiktok_web', 5, '{}', 'completed')"
        )
        self.database.conn.execute(
            "INSERT INTO actions(account_id, task_id, action_name, args, success) "
            "VALUES ('acc_test', 't1', 'platform.open_inbox', '{}', 1)"
        )
        self.database.conn.commit()
        self.reader = AgentDatabaseReader(self.schema.dsn, account_id="acc_test")
        self.addCleanup(self.reader.close)

    def test_the_reader_session_is_read_only_at_the_server(self):
        """`_query` refuses non-SELECT; the server refuses writes as well."""
        import psycopg

        connection = self.reader.connect()
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute(
                "UPDATE tasks SET status='pending' WHERE account_id='acc_test'"
            )

    def test_reads_tasks_and_activity(self):
        self.assertEqual([t["id"] for t in self.reader.tasks()], ["t1"])
        self.assertEqual(self.reader.counts()["tasks_completed"], 1)
        self.assertEqual(self.reader.activity()[0]["action_name"], "platform.open_inbox")

    def test_task_detail_includes_its_actions(self):
        task = self.reader.task("t1")
        self.assertIsNotNone(task)
        self.assertEqual(len(task["actions"]), 1)

    def test_a_write_is_refused_rather_than_executed(self):
        with self.assertRaises(ReadOnlyViolation):
            self.reader._query(
                "UPDATE tasks SET status='pending' WHERE account_id = 'acc_test'"
            )
        # And the row really is untouched.
        self.assertEqual(self.reader.tasks()[0]["status"], "completed")

    def test_an_unreachable_database_reads_as_absent_not_as_an_error(self):
        missing = AgentDatabaseReader(
            "postgresql://agent:agent@127.0.0.1:1/agent", account_id="acc_test"
        )
        self.assertFalse(missing.health()["ok"])
        self.assertEqual(missing.tasks(), [])

    def test_a_dashboard_never_sees_another_account_in_the_shared_database(self):
        """Both accounts live in one database; only the filter separates them."""
        self.database.register_account()
        # Written as the other shop's own process would: this account's
        # connection is not allowed to (row-level security).
        other_shop = unscoped_connection(self, self.database)
        other_shop.execute(
            "INSERT INTO accounts(id, platform) VALUES ('acc_other', 'tiktok_web') "
            "ON CONFLICT(id) DO NOTHING"
        )
        other_shop.execute(
            "INSERT INTO tasks("
            "id, account_id, capability, platform, priority, payload, status) "
            "VALUES ('t-other', 'acc_other', 'open_inbox', 'tiktok_web', 5, '{}', 'completed')"
        )

        self.assertEqual([t["id"] for t in self.reader.tasks()], ["t1"])

        other = AgentDatabaseReader(self.schema.dsn, account_id="acc_other")
        self.addCleanup(other.close)
        self.assertEqual([t["id"] for t in other.tasks()], ["t-other"])
        self.assertIsNone(other.task("t1"))
        self.assertIsNone(self.reader.task("t-other"))


@requires_postgres
class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.schema = TemporaryPostgresSchema()
        self.store = NotificationStore(self.schema.dsn)

    def tearDown(self):
        self.store.close()
        self.schema.cleanup()

    def test_a_flapping_check_updates_one_row_instead_of_flooding(self):
        for _ in range(5):
            self.store.from_event({
                "type": "health.changed", "account_id": "a",
                "payload": {"component": "control", "ok": False, "detail": "down"},
            })
        self.assertEqual(len(self.store.list()), 1)

    def test_a_recovered_check_closes_its_notification(self):
        self.store.from_event({
            "type": "health.changed", "account_id": "a",
            "payload": {"component": "control", "ok": False, "detail": "down"},
        })
        self.assertEqual(self.store.open_count(), 1)
        self.store.from_event({
            "type": "health.changed", "account_id": "a",
            "payload": {"component": "control", "ok": True},
        })
        self.assertEqual(self.store.open_count(), 0)

    def test_escalations_from_the_extension_runtime_are_persisted(self):
        # UI handoffs are events even when reply history persistence is on;
        # without the supervisor, a CAPTCHA would leave no durable trace.
        self.store.from_event({
            "type": "escalation.opened", "account_id": "a",
            "payload": {"session_id": "tab-1", "reason": "security_challenge"},
        })
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"], "security_challenge")

    def test_routine_events_are_not_notifications(self):
        self.store.from_event({
            "type": "agent.paused", "account_id": "a", "payload": {},
        })
        self.assertEqual(self.store.open_count(), 0)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.submitted = []
        self.finished = {}
        # The real poll interval paces requests against a live agent; the tests
        # only care about ordering, so they do not wait it out.
        patcher = unittest.mock.patch(
            "dashboard_api.orchestrator.POLL_SECONDS", 0.01
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _submit(self, account_id, capability, payload):
        self.submitted.append((account_id, capability))
        return f"task-{len(self.submitted)}"

    async def _status(self, account_id, task_id):
        return self.finished.get(task_id)

    def _orchestrator(self):
        return Orchestrator(self._submit, self._status, lambda *a, **k: None)

    async def test_sequential_waits_for_each_account_before_the_next(self):
        orchestrator = self._orchestrator()
        # Both tasks report completed as soon as they are polled.
        self.finished = {
            "task-1": {"status": "completed"}, "task-2": {"status": "completed"},
        }
        plan = orchestrator.start("sequential", [
            {"account_id": "a", "capability": "open_inbox"},
            {"account_id": "b", "capability": "open_inbox"},
        ])
        await orchestrator._tasks[plan.id]
        self.assertEqual(plan.status, "completed")
        self.assertEqual(self.submitted, [("a", "open_inbox"), ("b", "open_inbox")])

    async def test_a_failed_task_fails_its_step_and_the_plan(self):
        orchestrator = self._orchestrator()
        self.finished = {"task-1": {"status": "failed", "error": "boom"}}
        plan = orchestrator.start(
            "parallel", [{"account_id": "a", "capability": "open_inbox"}]
        )
        await orchestrator._tasks[plan.id]
        self.assertEqual(plan.steps[0].status, "failed")
        self.assertEqual(plan.steps[0].error, "boom")
        self.assertEqual(plan.status, "failed")


class ProductRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        product = self.directory / "products" / "P1"
        (product / "images").mkdir(parents=True)
        (product / "videos").mkdir()
        (product / "product.json").write_text(
            '{"id": "P1", "name": "Sữa Milo", "category": "Sữa", '
            '"description": "sữa lúa mạch", "benefits": ["bổ sung năng lượng"], '
            '"stock": 7, "media": {"images": ["images/a.jpg"], '
            '"videos": ["videos/b.mp4"]}}',
            encoding="utf-8",
        )
        (product / "images" / "a.jpg").write_bytes(b"x")
        (product / "videos" / "b.mp4").write_bytes(b"y")
        (self.directory / "products" / "SP999").mkdir()
        self.repository = ProductRepository(self.directory / "products")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_declared_media_is_resolved_from_the_product_directory(self):
        product = self.repository.get_by_id("P1")
        self.assertEqual(len(product["media"]["images"]), 1)
        self.assertEqual(len(product["media"]["videos"]), 1)

    def test_directory_without_product_json_is_reported(self):
        invalid = self.repository.invalid_product_directories()
        self.assertEqual(invalid, [{
            "directory": "SP999", "reason": "missing_product_json"
        }])

    def test_search_matches_fields_that_actually_exist(self):
        self.assertEqual([p["id"] for p in self.repository.search("sữa")], ["P1"])
        self.assertEqual(self.repository.search("xe máy"), [])

    def test_get_product_returns_empty_for_unknown_so_the_validator_blocks(self):
        self.assertEqual(asyncio.run(self.repository.get_product("KHONG_CO")), {})

    def test_inventory_comes_from_verified_product_data(self):
        result = asyncio.run(self.repository.check_inventory("P1"))
        self.assertEqual(result["stock"], 7)
        self.assertTrue(result["available"])

    def test_size_inventory_requires_explicit_variants(self):
        self.assertEqual(
            asyncio.run(self.repository.check_inventory("P1", size="M")), {}
        )

    def test_product_id_cannot_escape_product_root(self):
        self.assertIsNone(self.repository.get_by_id("../outside"))

    def test_business_tool_marks_an_empty_lookup_as_unverified(self):
        tool = ServiceMethodTool("get_product", self.repository, "get_product")
        result = asyncio.run(tool.execute({"product_id": "KHONG_CO"}))
        self.assertFalse(result.success)
        self.assertEqual(result.data, {})


class OutboundAttachmentTests(unittest.TestCase):
    BASE = dict(
        platform="tiktok_web", conversation_id="c1",
        customer_id="u1", source_event_id="e1",
    )

    def test_an_image_only_message_can_be_built(self):
        message = OutboundMessage(
            attachments=[Attachment(kind="video", path="/x/demo.mp4")], **self.BASE
        )
        self.assertEqual(message.text, "")
        self.assertEqual(len(message.attachments), 1)

    def test_a_message_with_neither_text_nor_attachment_is_refused(self):
        with self.assertRaises(ValueError):
            OutboundMessage(**self.BASE)



class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "config.yaml"
        shutil.copy(BASE_DIR / "configs" / "config.yaml", self.path)
        self.config = load_config(self.path)
        self.skill = InboxScanSkill(
            reply_quiet_period_seconds=20, reply_wait_timeout_seconds=30
        )

        class FakeAgent:
            pass

        self.agent = FakeAgent()
        self.agent.inbox_skill = self.skill
        self.agent.scenario_policy = ScenarioPolicy()
        self.agent.followup_policy = FollowUpPolicy()

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_whitelisted_values_take_effect_on_the_live_object(self):
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.auto_check.reply_quiet_period_seconds": 12,
        })
        self.assertEqual(self.skill.reply_quiet_period_seconds, 12)
        self.assertIn("messaging.auto_check.reply_quiet_period_seconds", result["applied"])

    def test_boot_time_values_are_reported_as_restart_required(self):
        result = apply_runtime_settings(self.agent, self.config, {
            "browser.cdp_url": "http://127.0.0.1:29300",
        })
        self.assertIn("browser.cdp_url", result["restart_required"])
        self.assertEqual(result["applied"], {})

    def test_the_removed_reply_mode_is_rejected_not_silently_accepted(self):
        """Nothing has read it since the pipeline became the only reply
        provider; accepting it told an operator "set to human" while the
        agent kept answering customers."""
        result = apply_runtime_settings(self.agent, self.config, {
            "extension_bridge.reply.mode": "human",
        })
        self.assertIn("extension_bridge.reply.mode", result["rejected"])

    def test_unknown_keys_are_rejected_and_never_written(self):
        result = apply_runtime_settings(self.agent, self.config, {"nope.at.all": 1})
        self.assertIn("nope.at.all", result["rejected"])
        self.assertIsNone(load_config(self.path).get("nope.at.all"))

    def test_applied_values_survive_a_restart(self):
        apply_runtime_settings(self.agent, self.config, {
            "messaging.auto_check.max_conversations_per_scan": 4,
        })
        reloaded = load_config(self.path)
        self.assertEqual(
            reloaded.get("messaging.auto_check.max_conversations_per_scan"), 4
        )
        # Unrelated config is preserved by the write-back.
        self.assertEqual(reloaded.get("policy.limits.max_actions_per_task"), 200)

    def test_scenario_examples_take_effect_on_the_shared_policy_and_persist(self):
        examples = [{"customer": "Hi", "agent": "Dạ chào bạn ạ."}]
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.scenario_examples": examples,
        })
        self.assertEqual(self.agent.scenario_policy.examples, examples)
        self.assertIn("messaging.scenario_examples", result["applied"])
        self.assertEqual(
            load_config(self.path).get("messaging.scenario_examples"), examples,
        )

    def test_scenario_setter_reports_missing_policy_instead_of_crashing(self):
        del self.agent.scenario_policy
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.scenario_examples": [],
        })
        self.assertIn("messaging.scenario_examples", result["rejected"])

    def test_scenario_setter_rejects_a_non_list_value(self):
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.scenario_examples": "not a list",
        })
        self.assertIn("messaging.scenario_examples", result["rejected"])

    def test_reply_style_reaches_both_generators_live_and_persists(self):
        weak, strong = SimpleNamespace(style_rules=None), SimpleNamespace(style_rules=None)
        self.skill.reply_provider = SimpleNamespace(
            reply_generator=weak, strong_reply_generator=strong,
        )
        rules = ["Gọi khách là bạn, xưng mình.", "  "]
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.reply_style": rules,
        })
        self.assertIn("messaging.reply_style", result["applied"])
        self.assertEqual(weak.style_rules, rules)
        self.assertEqual(strong.style_rules, rules)
        self.assertEqual(load_config(self.path).get("messaging.reply_style"), rules)

    def test_reply_style_setter_rejects_without_a_reply_provider(self):
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.reply_style": ["x"],
        })
        self.assertIn("messaging.reply_style", result["rejected"])

    def test_reply_language_reaches_both_generators_live_and_persists(self):
        weak = SimpleNamespace(reply_language="vi")
        strong = SimpleNamespace(reply_language="vi")
        self.skill.reply_provider = SimpleNamespace(
            reply_generator=weak, strong_reply_generator=strong,
        )
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.reply_language": "my",
        })
        self.assertIn("messaging.reply_language", result["applied"])
        self.assertEqual(weak.reply_language, "my")
        self.assertEqual(strong.reply_language, "my")
        self.assertEqual(load_config(self.path).get("messaging.reply_language"), "my")

    def test_an_unsupported_reply_language_is_rejected_and_not_persisted(self):
        self.skill.reply_provider = SimpleNamespace(
            reply_generator=SimpleNamespace(reply_language="vi"),
            strong_reply_generator=None,
        )
        before = load_config(self.path).get("messaging.reply_language")
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.reply_language": "klingon",
        })
        self.assertIn("messaging.reply_language", result["rejected"])
        # The configured language is left exactly as it was.
        self.assertEqual(load_config(self.path).get("messaging.reply_language"), before)

    def test_followup_rules_take_effect_on_the_shared_policy_and_persist(self):
        rules = [{"delay_minutes": 60, "message": "Anh/chị còn cần shop hỗ trợ không ạ?"}]
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.follow_up.rules": rules,
        })
        self.assertEqual(len(self.agent.followup_policy.rules), 1)
        self.assertEqual(
            self.agent.followup_policy.rules[0]["message"],
            "Anh/chị còn cần shop hỗ trợ không ạ?",
        )
        self.assertIn("messaging.follow_up.rules", result["applied"])
        persisted = load_config(self.path).get("messaging.follow_up.rules")
        self.assertEqual(len(persisted), 1)

    def test_followup_setter_reports_missing_policy_instead_of_crashing(self):
        del self.agent.followup_policy
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.follow_up.rules": [],
        })
        self.assertIn("messaging.follow_up.rules", result["rejected"])

    def test_followup_setter_rejects_a_non_list_value(self):
        result = apply_runtime_settings(self.agent, self.config, {
            "messaging.follow_up.rules": "not a list",
        })
        self.assertIn("messaging.follow_up.rules", result["rejected"])


class PauseTests(unittest.TestCase):
    def test_pause_reports_pending_while_a_task_is_still_running(self):
        class State:
            running = True
            paused = False
            pause_reason = ""
            current_task_id = "task-1"

        class Agent:
            state = State()

        status = RuntimeStatus(account_id="a", runtime="langgraph", agent=Agent())
        result = status.set_paused(True, "operator")
        # Honest reporting: the flag is set but the agent has not stopped yet.
        self.assertTrue(result["paused"])
        self.assertTrue(result["pending"])

    def test_pause_is_immediate_when_idle(self):
        class State:
            running = True
            paused = False
            pause_reason = ""
            current_task_id = None

        class Agent:
            state = State()

        status = RuntimeStatus(account_id="a", runtime="langgraph", agent=Agent())
        self.assertFalse(status.set_paused(True, "operator")["pending"])


class DrainTests(unittest.TestCase):
    """Draining is what makes restarting an agent safe to script.

    The agent owns one Chrome session and its side effects cannot be rolled
    back - a message already on its way to a customer is gone. So a deploy
    must refuse the next task, let the current one finish, and only then stop.
    `deploy/update.sh` refuses to stop a workspace that reports `drained:
    false`.
    """

    class State:
        running = True
        paused = False
        pause_reason = ""
        current_task_id = None

    def _server(self, state):
        from control.events import EventBus
        from control.auth import TOKEN_HEADER
        from control.server import ControlServer

        status = RuntimeStatus(
            account_id="a", runtime="langgraph",
            agent=SimpleNamespace(state=state),
        )
        server = ControlServer(status, EventBus("a"))
        return status, TestClient(server.app, headers={TOKEN_HEADER: server.token})

    def test_an_idle_agent_drains_at_once(self):
        state = self.State()
        status, client = self._server(state)

        body = client.post("/control/drain", json={"timeout_seconds": 5}).json()

        self.assertTrue(body["drained"])
        self.assertFalse(body["in_flight"])
        # Paused, so nothing new is claimed between here and the stop.
        self.assertTrue(body["paused"])
        self.assertTrue(status.paused)

    def test_a_task_that_finishes_mid_drain_is_waited_for(self):
        state = self.State()
        state.current_task_id = "task-1"
        status, client = self._server(state)
        finisher = threading.Timer(0.4, lambda: setattr(state, "current_task_id", None))
        finisher.start()
        self.addCleanup(finisher.cancel)

        body = client.post("/control/drain", json={"timeout_seconds": 10}).json()

        self.assertTrue(body["drained"])
        self.assertGreater(body["waited_seconds"], 0.3)

    def test_a_task_that_never_finishes_reports_a_failed_drain(self):
        """And stays paused: stopping anyway has to be a deliberate act, not
        something a script does by falling through."""
        state = self.State()
        state.current_task_id = "task-1"
        status, client = self._server(state)

        body = client.post("/control/drain", json={"timeout_seconds": 0.5}).json()

        self.assertFalse(body["drained"])
        self.assertTrue(body["in_flight"])
        self.assertTrue(status.paused)
        self.assertEqual(status.pause_reason, "drain")

    def test_the_reason_reaches_the_agent_state(self):
        state = self.State()
        status, client = self._server(state)

        client.post("/control/drain", json={"reason": "deploy"})

        self.assertEqual(state.pause_reason, "deploy")

    def test_in_flight_is_reported_whether_paused_or_not(self):
        """`RuntimeStatus.in_flight` is the one place that answers "is the
        agent genuinely idle" - the drain and the dashboard both read it."""
        state = self.State()
        status, _ = self._server(state)
        self.assertFalse(status.in_flight)

        state.current_task_id = "task-1"
        self.assertTrue(status.in_flight)


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_late_subscriber_receives_the_current_state(self):
        bus = EventBus("acct")
        bus.publish("agent.paused", paused=True)
        queue = bus.subscribe()
        event = queue.get_nowait()
        self.assertEqual(event.type, "agent.paused")

    async def test_a_slow_subscriber_drops_old_events_instead_of_blocking(self):
        bus = EventBus("acct")
        queue = bus.subscribe()
        for index in range(400):        # more than QUEUE_SIZE
            bus.publish("task.started", index=index)
        # Publishing never raised, and the newest event is still there.
        newest = None
        while not queue.empty():
            newest = queue.get_nowait()
        self.assertEqual(newest.payload["index"], 399)


if __name__ == "__main__":
    unittest.main()
