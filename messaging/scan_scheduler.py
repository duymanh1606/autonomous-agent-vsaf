import asyncio
import logging
from uuid import uuid4

from models.tasks import Task


logger = logging.getLogger(__name__)


class InboxScanScheduler:
    """Periodically schedules a policy-controlled inbox scan task.

    It schedules; it never reads a message. The skill that does the reading is
    `platforms.tiktok.inbox_watcher.InboxScanSkill`, and while both files were
    called `inbox_watcher.py` the two were easy to mistake for each other.
    """

    capability = "messaging.scan_inbox"

    def __init__(self, task_repository, interval_seconds=10, platform="tiktok_web",
                 completed_task_retention=100, schedule=None, state=None):
        self.task_repository = task_repository
        self.interval_seconds = max(2.0, float(interval_seconds))
        self.platform = platform
        self.completed_task_retention = max(1, int(completed_task_retention))
        # Operating hours. Outside them no new scan is scheduled; a scan already
        # running is left alone so a customer is never dropped mid-answer.
        self.schedule = schedule
        # AgentState, shared with the graph loop. Checked so this scheduler
        # stops enqueueing messaging.scan_inbox tasks while the agent is
        # paused for auto-sleep (dashboard_api's idle sweeper) - Chrome is
        # stopped in that state, so a scan task would just fail and pile up
        # in the queue instead of ever being claimed.
        self.state = state
        self._closed_logged = False
        self._paused_logged = False

    def _accepting_work(self):
        if self.state is not None and self.state.paused:
            if not self._paused_logged:
                logger.info(
                    "[INBOX] agent paused (%s); not scheduling new scans",
                    self.state.pause_reason or "no reason given",
                )
                self._paused_logged = True
            return False
        self._paused_logged = False
        if self.schedule is None or self.schedule.is_open():
            self._closed_logged = False
            return True
        if not self._closed_logged:
            logger.info(
                "[INBOX] outside operating hours (%s); not scheduling new scans",
                self.schedule.describe(),
            )
            self._closed_logged = True
        return False

    async def run(self):
        while True:
            try:
                if not self._accepting_work():
                    await asyncio.sleep(self.interval_seconds)
                    continue
                await self.task_repository.prune_completed_capability(
                    self.capability, self.completed_task_retention
                )
                if not await self.task_repository.has_active_capability(self.capability):
                    await self.task_repository.submit(Task(
                        id=f"inbox-scan-{uuid4()}",
                        account_id=self.task_repository.account_id,
                        capability=self.capability,
                        platform=self.platform,
                        priority=3,
                        payload={"automatic": True},
                    ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "inbox_watcher_schedule_failed error_type=%s",
                    type(exc).__name__, exc_info=True,
                )
            await asyncio.sleep(self.interval_seconds)
