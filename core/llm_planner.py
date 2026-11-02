import asyncio
import json
import logging
import re

from models.action import Action


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
logger = logging.getLogger(__name__)


class LlmPlanner:
    """Asks the model for the next action when no skill claims the task.

    Named for what it is: the retired extension runtime's `UIPlanner` was
    the deterministic
    planner of the extension runtime, and the two used to sit next to each
    other as `planner.py` and `planer.py` - one letter apart, opposite in kind.
    """

    def __init__(
        self, client, tools, platform, temperature=0.2, available_actions=None,
        reasoning_progress_seconds=10,
    ):
        self.client = client
        self.tools = tools
        self.platform = platform
        self.temperature = temperature
        self.available_actions = tuple(available_actions or tools.names)
        self.reasoning_progress_seconds = max(0.0, float(reasoning_progress_seconds))

    async def plan(self, task, observation, memory):
        deterministic = self.platform.next_action(task, observation)
        if deterministic:
            logger.info(
                "Planner selected deterministic action: task_id=%s action=%s",
                task.id, deterministic.name,
            )
            return deterministic
        if self.client is None:
            logger.warning("Planner unavailable: task_id=%s", task.id)
            return Action(name="abort_task", args={"reason": "No deterministic route and planner is disabled"})

        available = list(self.available_actions) + ["abort_task"]
        prompt = (
            "Choose exactly one safe next action. Return one JSON object only with keys "
            "name, args, reason, confidence. Never output code or shell commands. "
            "Never enter usernames, passwords, OTPs, recovery codes, or solve CAPTCHA/security verification. "
            "Authentication is handled manually and planning is allowed only after LOGGED_IN.\n"
            f"TASK: {task.model_dump_json()}\n"
            f"CURRENT SCREEN: {observation.screen}\n"
            f"AUTH STATE: {observation.auth_state.value}\n"
            f"OBSERVATION: {observation.model_dump_json(exclude={'raw_ui_xml'})}\n"
            f"RELEVANT MEMORY: {json.dumps(memory.relevant(observation.screen), ensure_ascii=False)}\n"
            f"AVAILABLE ACTIONS: {json.dumps(available)}"
        )
        logger.info(
            "Agent reasoning started: task_id=%s source=planner_model screen=%s auth=%s",
            task.id, observation.screen, observation.auth_state.value,
        )
        raw = await self._generate_with_progress(task.id, prompt)
        match = JSON_BLOCK.search(raw)
        if not match:
            raise ValueError("Planner did not return JSON")
        action = Action.model_validate(json.loads(match.group(0)))
        if action.name not in available:
            raise ValueError(f"Planner selected unavailable action: {action.name}")
        logger.info(
            "Planner model response accepted: task_id=%s action=%s confidence=%.2f",
            task.id, action.name, action.confidence,
        )
        return action

    async def _generate_with_progress(self, task_id, prompt):
        request = asyncio.create_task(self.client.generate(
            prompt, temperature=self.temperature, json_mode=True
        ))
        elapsed = 0.0
        try:
            if self.reasoning_progress_seconds <= 0:
                return await request
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(request),
                        timeout=self.reasoning_progress_seconds,
                    )
                except asyncio.TimeoutError:
                    elapsed += self.reasoning_progress_seconds
                    logger.info(
                        "Agent reasoning in progress: task_id=%s source=planner_model elapsed_seconds=%s",
                        task_id, int(elapsed),
                    )
        finally:
            if not request.done():
                request.cancel()
