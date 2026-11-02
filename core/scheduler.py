class Scheduler:

    def __init__(
        self,
        task_repository
    ):
        self.tasks = task_repository

    async def next_task(self, *, human_only=False):
        if human_only:
            return await self.tasks.get_next_task(human_only=True)
        return await self.tasks.get_next_task()
