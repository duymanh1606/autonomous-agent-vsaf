class SkillRegistry:

    def __init__(self):
        self.skills = []

    def register(self, skill):
        if any(existing.name == skill.name for existing in self.skills):
            raise ValueError(f"Skill already registered: {skill.name}")
        self.skills.append(skill)

    def find_by_capability(
        self,
        capability
    ):

        for skill in self.skills:

            if (
                capability
                in skill.capabilities
            ):
                return skill

        return None
