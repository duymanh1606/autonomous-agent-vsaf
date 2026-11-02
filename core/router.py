class TaskRouter:

    def __init__(
        self,
        skill_registry
    ):
        self.skills = skill_registry

    async def route(self, task):
        skill = self.skills.find_by_capability(task.capability)
        if skill and await skill.can_handle(task, None):
            return skill
        return None
