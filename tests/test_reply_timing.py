"""Where the time goes while one customer message is answered.

Each reply the pipeline runs for gets one `[TIMING]` log line and one
`reply_timings` row: open, hold, reply (with the pipeline's own steps),
pre-send recheck, send and verify. A thread that needed no reply records
nothing, and a failed write never fails the scan.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.message_router import MessageRouter
from agent.reply_service import ConversationContext, PipelineReplyProvider, ReplyDecision
from agent.reply_timing import ReplyTimer
from agent.reply_validator import ReplyValidator
from agent.state_manager import CustomerStateManager
from models.result import ActionResult
from platforms.tiktok.inbox_watcher import InboxScanSkill
from platforms.tiktok.screens import TikTokScreen
from storage.reply_timings import ReplyTimingRepository
from tests.support import make_database
from tests.test_inbox_scan_flow import (
    StubContext, StubReplyProvider, StubReplySkill, observation_for, scan_task,
)
from tests.test_reply_pipeline_parity import (
    STAGES, StubAnalyzer, StubGenerator, StubStore, analysis,
)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class RecordingTimings:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    async def record(self, **row):
        if self.fail:
            raise RuntimeError("database went away")
        self.rows.append(row)


class TimerTests(unittest.TestCase):
    def test_a_repeated_phase_adds_up_and_total_covers_everything(self):
        clock = FakeClock()
        timer = ReplyTimer(clock=clock)
        with timer.phase("llm_generate"):
            clock.now += 1.5
        clock.now += 0.25  # time outside any phase still counts in total
        with timer.phase("llm_generate"):  # the rewrite after a refused draft
            clock.now += 0.5

        self.assertEqual(
            timer.milliseconds(), {"llm_generate": 2000, "total": 2250}
        )

    def test_the_log_line_lists_the_steps_in_the_order_they_run(self):
        clock = FakeClock()
        timer = ReplyTimer(clock=clock)
        for name in ("verify", "llm_analyze", "open", "reply"):
            timer.add(name, 1)
        clock.now += 4

        self.assertEqual(
            timer.summary(),
            "open=1.00s reply=1.00s verify=1.00s llm_analyze=1.00s total=4.00s",
        )


def _scan_context(messages):
    opened = observation_for(TikTokScreen.CONVERSATION, messages=messages, published=1)
    return StubContext(
        observation_for(TikTokScreen.MESSAGES),
        {
            "platform.list_unread_conversations": ActionResult(
                success=True, data={"tool": {"conversations": ["customer"]}}
            ),
            "platform.open_unread_conversation": (
                ActionResult(success=True, data={"tool": {"success": True}}),
                opened,
            ),
        },
    )


UNANSWERED = [{"id": "m1", "text": "Còn hàng không?", "direction": "INBOUND"}]


class ScanTimingTests(unittest.IsolatedAsyncioTestCase):
    def _skill(self, timings, verify_seconds=0.0):
        provider = StubReplyProvider(ReplyDecision(
            text="Dạ còn hàng ạ", should_send=True, reason="generated", model="stub"
        ))
        reply_skill = StubReplySkill(ActionResult(success=True, data={
            "decision": "complete_task", "send_status": "SENT",
            "verify_seconds": verify_seconds,
        }))
        return InboxScanSkill(
            reply_provider=provider, reply_skill=reply_skill, timings=timings,
        )

    async def test_a_sent_reply_records_every_step(self):
        timings = RecordingTimings()
        with self.assertLogs("platforms.tiktok.inbox_watcher", "INFO") as logs:
            result = await self._skill(timings).execute(
                scan_task(), _scan_context(UNANSWERED)
            )

        self.assertEqual(result.data["conversations_handled"][0]["status"], "sent")
        self.assertEqual(len(timings.rows), 1)
        row = timings.rows[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["task_id"], "inbox-scan-1")
        self.assertEqual(row["burst_size"], 1)
        for step in ("open", "reply", "send", "verify", "total"):
            self.assertIn(step, row["milliseconds"])
        self.assertTrue(any("[TIMING]" in line and "status=sent" in line for line in logs.output))

    async def test_verify_is_carved_out_of_the_send_task(self):
        """The messaging skill's read-back cannot exceed the send task it ran in."""
        timings = RecordingTimings()
        await self._skill(timings, verify_seconds=3600.0).execute(
            scan_task(), _scan_context(UNANSWERED)
        )

        milliseconds = timings.rows[0]["milliseconds"]
        self.assertEqual(milliseconds["send"], 0)
        self.assertLess(milliseconds["verify"], 3600 * 1000)

    async def test_a_thread_that_needed_no_reply_records_nothing(self):
        timings = RecordingTimings()
        answered = UNANSWERED + [{"id": "m2", "text": "Dạ còn ạ", "direction": "OUTBOUND"}]
        await self._skill(timings).execute(scan_task(), _scan_context(answered))

        self.assertEqual(timings.rows, [])

    async def test_a_failed_write_does_not_fail_the_scan(self):
        with self.assertLogs("platforms.tiktok.inbox_watcher", "ERROR"):
            result = await self._skill(RecordingTimings(fail=True)).execute(
                scan_task(), _scan_context(UNANSWERED)
            )

        self.assertTrue(result.success)
        self.assertEqual(result.data["replies_sent"], 1)


class PipelineTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_pipeline_times_its_own_steps_on_the_callers_timer(self):
        provider = PipelineReplyProvider(
            analyzer=StubAnalyzer(analysis()),
            router=MessageRouter(),
            state_manager=CustomerStateManager(STAGES),
            reply_generator=StubGenerator(),
            reply_validator=ReplyValidator(500),
            conversation_store=StubStore(),
        )
        timer = ReplyTimer()
        context = ConversationContext(
            platform="tiktok_web", conversation_key="thread-1", message_id="m1",
            message_text="Shop ơi", customer_name="khách",
            history=[{"id": "m1", "text": "Shop ơi", "direction": "INBOUND"}],
            pending_messages=[{"id": "m1", "text": "Shop ơi", "index": 0}],
            timer=timer,
        )

        decision = await provider.reply(context)

        self.assertTrue(decision.should_send)
        for step in ("llm_prepare", "llm_analyze", "llm_generate", "llm_validate"):
            self.assertTrue(timer.has(step), step)


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_steps_get_columns_and_the_rest_goes_to_detail(self):
        database = make_database(self)
        await ReplyTimingRepository(database).record(
            conversation_key="thread-1", task_id="scan-1", status="sent", burst_size=2,
            milliseconds={
                "open": 400, "hold": 10200, "reply": 2100, "send": 900,
                "verify": 600, "llm_generate": 1500, "total": 14300,
            },
        )

        row = database.conn.execute(
            "SELECT * FROM reply_timings WHERE account_id = %s", (database.account_id,)
        ).fetchone()
        self.assertEqual(row["account_id"], "acc_test")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["burst_size"], 2)
        self.assertEqual(
            (row["open_ms"], row["hold_ms"], row["reply_ms"], row["recheck_ms"],
             row["send_ms"], row["verify_ms"], row["total_ms"]),
            (400, 10200, 2100, None, 900, 600, 14300),
        )
        self.assertEqual(row["detail"], {"llm_generate": 1500})


if __name__ == "__main__":
    unittest.main()
