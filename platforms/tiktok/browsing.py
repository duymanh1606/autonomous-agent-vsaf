import asyncio

from models.action import Action
from models.result import ActionResult
from platforms.tiktok.screens import TikTokScreen
from skills.base import Skill


class BrowseFeedSkill(Skill):
    name = "browse_feed"
    capabilities = ["browse_feed", "inspect_video"]

    async def can_handle(self, task, context):
        count = task.payload.get("count", 1)
        return isinstance(count, int) and 1 <= count <= 20

    async def execute(self, task, context):
        if context.observation.screen != TikTokScreen.VIDEO_FEED:
            opened = await context.execute_action(Action(
                name="platform.open_home", reason="Browse only from the verified video feed"
            ))
            if not opened.success:
                return opened

        count = task.payload.get("count", 1)
        wait_seconds = max(0.0, min(float(task.payload.get("wait_seconds", 0)), 10.0))
        videos = [self._visible_text(context.observation)]
        for _ in range(1, count):
            if wait_seconds:
                await asyncio.sleep(wait_seconds)
            result = await context.execute_action(Action(
                name="platform.swipe_next", reason="Advance one video using the deterministic controller"
            ))
            if not result.success:
                return result
            videos.append(self._visible_text(context.observation))
        return ActionResult(success=True, data={"videos": videos, "count": len(videos)})

    @staticmethod
    def _visible_text(observation):
        return [
            item.get("text", "").strip() for item in observation.elements
            if item.get("text", "").strip()
        ]
