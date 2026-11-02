import asyncio
import logging


logger = logging.getLogger(__name__)


class MessageRuntime:
    """The agent's background loops: the inbox watcher, whose scan answers
    each customer in place through the full reply pipeline, and the
    follow-up scheduler.

    There used to be a second, queue-driven way to answer a customer (a
    processor answering events off an incoming queue, an outbound worker
    turning its replies into send tasks). Its event ids never matched the ids
    the scan settles by, so the two could answer one message twice; it was
    unwired and then removed. The scan is the only reply path.
    """

    def __init__(self, inbox_watcher=None, followup_scheduler=None):
        self._tasks = []
        self.inbox_watcher = inbox_watcher
        self.followup_scheduler = followup_scheduler

    async def start(self):
        if self._tasks:
            return
        if self.inbox_watcher is not None:
            self._tasks.append(asyncio.create_task(
                self.inbox_watcher.run(), name="tiktok-inbox-watcher"
            ))
        if self.followup_scheduler is not None:
            self._tasks.append(asyncio.create_task(
                self.followup_scheduler.run(), name="tiktok-followup-scheduler"
            ))

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
