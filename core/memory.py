from collections import deque


class Memory:
    def __init__(self, repository=None, max_actions=500):
        self.repository = repository
        self.actions = deque(maxlen=max_actions)
        self.transitions = {}

    async def record_action(self, task_id, action, result, before=None, after=None):
        entry = {
            "task_id": task_id,
            "action": action.name,
            "args": action.args,
            "success": result.success,
            "error": result.error,
            "before": before,
            "after": after,
        }
        self.actions.append(entry)
        if before and after:
            key = (before, action.name)
            aggregate = self.transitions.setdefault(key, {"success": 0, "failure": 0, "targets": {}})
            bucket = "success" if result.success else "failure"
            aggregate[bucket] += 1
            aggregate["targets"][after] = aggregate["targets"].get(after, 0) + 1
        if self.repository:
            await self.repository.record_action(task_id, action, result)
            if before and after:
                await self.repository.record_transition(before, action.name, after, 1.0 if result.success else 0.0)

    async def record_task(self, task, result):
        if self.repository:
            await self.repository.complete(
                task.id, result.success, result.error, result.data, task=task,
            )

    def relevant(self, screen=None, limit=20):
        entries = list(self.actions)
        if screen:
            entries = [item for item in entries if item["before"] == screen]
        return entries[-limit:]
