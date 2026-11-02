import logging

import httpx

from storage.llm_usage import record_usage


logger = logging.getLogger(__name__)


class GeminiVisionClient:
    """Minimal Gemini GenerateContent client for screenshot understanding."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gemini-3.1-flash-lite",
        timeout: float = 120,
        transport=None,
        account_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport
        self.account_id = account_id
        self.api_call_count = 0

    async def generate(
        self,
        prompt: str,
        temperature: float = 0.0,
        json_mode: bool = True,
        images: list[str] | None = None,
        response_schema: dict | None = None,
    ) -> str:
        if not self.api_key or self.api_key == "api_key":
            raise RuntimeError(
                "Gemini API key is missing. Export GEMINI_API_KEY before starting the agent."
            )
        if not images:
            raise ValueError("Gemini vision analysis requires at least one image")

        parts = [{"text": prompt}]
        parts.extend({
            "inlineData": {
                "mimeType": "image/png",
                "data": image,
            }
        } for image in images)
        generation_config = {}
        if not self.model.startswith("gemini-3."):
            generation_config["temperature"] = temperature
        if json_mode:
            generation_config["responseMimeType"] = "application/json"
            if response_schema is not None:
                generation_config["responseJsonSchema"] = response_schema
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            self.api_call_count += 1
            call_number = self.api_call_count
            logger.info(
                "API call: provider=gemini_vision model=%s operation=generate_content count=%s status=started",
                self.model, call_number,
            )
            try:
                response = await client.post(
                    f"{self.base_url}/models/{self.model}:generateContent",
                    headers={
                        "x-goog-api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                record_usage(self.account_id, "gemini", self.model, data.get("usageMetadata"))
            except Exception as exc:
                logger.warning(
                    "API call: provider=gemini_vision model=%s operation=generate_content count=%s status=failed error_type=%s",
                    self.model, call_number, type(exc).__name__,
                )
                raise
            logger.info(
                "API call: provider=gemini_vision model=%s operation=generate_content count=%s status=success http_status=%s",
                self.model, call_number, response.status_code,
            )

        try:
            response_parts = data["candidates"][0]["content"]["parts"]
            text = "".join(
                part.get("text", "") for part in response_parts
                if isinstance(part, dict)
            ).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini returned an invalid vision response") from exc
        if not text:
            raise RuntimeError("Gemini returned an empty vision response")
        return text
