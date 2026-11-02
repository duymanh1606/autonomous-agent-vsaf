import asyncio
import json
import logging
from typing import Any

import httpx

from llm.base import LLMProvider, StructuredModel
from storage.llm_usage import record_usage


logger = logging.getLogger(__name__)


class GeminiFlashLiteProvider(LLMProvider):
    """Small REST adapter for Gemini structured output with bounded retries."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.1-flash-lite",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        timeout_seconds: float = 30,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1,
        client: httpx.AsyncClient | None = None,
        account_id: str | None = None,
    ):
        if not api_key or api_key == "api_key":
            raise ValueError("GEMINI_API_KEY is required for message processing")
        self.api_key = api_key
        self.account_id = account_id
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._client = client
        self._owns_client = client is None
        self.api_call_count = 0

    @property
    def model_name(self) -> str:
        return self.model

    async def generate_structured(
        self, prompt: str, response_model: type[StructuredModel]
    ) -> StructuredModel:
        payload = self._payload(prompt)
        payload["generationConfig"].update({
            "responseMimeType": "application/json",
            "responseJsonSchema": response_model.model_json_schema(),
        })
        text = await self._request(payload)
        return response_model.model_validate_json(text)

    async def generate_text(self, prompt: str) -> str:
        return (await self._request(self._payload(prompt))).strip()

    async def generate(
        self,
        prompt: str,
        temperature: float | None = None,
        json_mode: bool = False,
        images: list[str] | None = None,
    ) -> str:
        """Compatibility surface for the legacy planner and messaging skill.

        Gemini remains the only text provider. Image requests use the dedicated
        vision client, where the prompt and screenshot are validated together.
        """
        if images:
            raise ValueError("Use GeminiVisionClient for image input")
        payload = self._payload(prompt)
        if temperature is not None and not self.model.startswith("gemini-3."):
            payload["generationConfig"]["temperature"] = float(temperature)
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        return (await self._request(payload)).strip()

    async def validate_model(self):
        """Fail startup before inbox ingestion when generation is unavailable."""
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        if self._client is None:
            self._client = client
        headers = {"x-goog-api-key": self.api_key}
        model_url = f"{self.base_url}/models/{self.model}"
        response = await self._get(client, model_url, headers, "model_get")
        if response.is_success:
            body = response.json()
            methods = set(body.get("supportedGenerationMethods", []))
            if "generateContent" not in methods:
                raise RuntimeError(
                    f"Gemini model {self.model!r} does not support generateContent"
                )
            probe = self._payload(
                "Return exactly the word READY. This is a startup availability probe."
            )
            probe["generationConfig"]["maxOutputTokens"] = 16
            await self._request(probe)
            return body

        available = []
        if response.status_code == 404:
            listed = await self._get(
                client, f"{self.base_url}/models", headers, "models_list",
                params={"pageSize": 1000},
            )
            if listed.is_success:
                available = [
                    str(item.get("name", "")).removeprefix("models/")
                    for item in listed.json().get("models", [])
                    if "generateContent" in item.get("supportedGenerationMethods", [])
                    and "flash" in str(item.get("name", "")).casefold()
                ]
        suffix = f" Available Flash models: {', '.join(available)}" if available else ""
        detail = self._safe_error_detail(response)
        raise RuntimeError(
            f"Gemini model preflight failed with HTTP {response.status_code} "
            f"for {self.model!r}: {detail}.{suffix}"
        )

    async def close(self):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(self, prompt: str) -> dict[str, Any]:
        generation_config = {"maxOutputTokens": self.max_output_tokens}
        if not self.model.startswith("gemini-3."):
            generation_config["temperature"] = self.temperature
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

    async def _request(self, payload: dict[str, Any]) -> str:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        if self._client is None:
            self._client = client
        url = f"{self.base_url}/models/{self.model}:generateContent"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.api_call_count += 1
            call_number = self.api_call_count
            logger.info(
                "API call: provider=gemini_message model=%s operation=generate_content count=%s attempt=%s status=started",
                self.model, call_number, attempt + 1,
            )
            try:
                response = await client.post(
                    url,
                    headers={
                        "x-goog-api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                record_usage(self.account_id, "gemini", self.model, body.get("usageMetadata"))
                parts = body["candidates"][0]["content"]["parts"]
                text = "".join(str(part.get("text", "")) for part in parts).strip()
                if not text:
                    logger.warning(
                        "API call: provider=gemini_message model=%s operation=generate_content count=%s attempt=%s status=failed error_type=EmptyResponse",
                        self.model, call_number, attempt + 1,
                    )
                    raise RuntimeError("Gemini returned an empty response")
                logger.info(
                    "API call: provider=gemini_message model=%s operation=generate_content count=%s attempt=%s status=success http_status=%s",
                    self.model, call_number, attempt + 1, response.status_code,
                )
                return text
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "API call: provider=gemini_message model=%s operation=generate_content count=%s attempt=%s status=failed http_status=%s",
                    self.model, call_number, attempt + 1, exc.response.status_code,
                )
                last_error = exc
                status = exc.response.status_code
                if status not in {408, 429} and status < 500:
                    detail = self._safe_error_detail(exc.response)
                    raise RuntimeError(
                        f"Gemini rejected model {self.model!r} with HTTP {status}: "
                        f"{detail}; verify the key's project access using models.list"
                    ) from exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_backoff_seconds * (2 ** attempt))
            except (
                httpx.TransportError, KeyError, IndexError, TypeError,
                json.JSONDecodeError,
            ) as exc:
                logger.warning(
                    "API call: provider=gemini_message model=%s operation=generate_content count=%s attempt=%s status=failed error_type=%s",
                    self.model, call_number, attempt + 1, type(exc).__name__,
                )
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_backoff_seconds * (2 ** attempt))
        raise RuntimeError("Gemini request failed after bounded retries") from last_error

    async def _get(self, client, url, headers, operation, **kwargs):
        self.api_call_count += 1
        call_number = self.api_call_count
        logger.info(
            "API call: provider=gemini_message model=%s operation=%s count=%s status=started",
            self.model, operation, call_number,
        )
        try:
            response = await client.get(url, headers=headers, **kwargs)
        except Exception as exc:
            logger.warning(
                "API call: provider=gemini_message model=%s operation=%s count=%s status=failed error_type=%s",
                self.model, operation, call_number, type(exc).__name__,
            )
            raise
        logger.info(
            "API call: provider=gemini_message model=%s operation=%s count=%s status=%s http_status=%s",
            self.model, operation, call_number,
            "success" if response.is_success else "http_error",
            response.status_code,
        )
        return response

    def _safe_error_detail(self, response: httpx.Response) -> str:
        try:
            error = response.json().get("error", {})
            status = str(error.get("status") or "UNKNOWN")
            message = " ".join(str(error.get("message") or "request rejected").split())
            reasons = [
                str(detail.get("reason"))
                for detail in error.get("details", [])
                if isinstance(detail, dict)
                and detail.get("reason")
            ]
            safe = f"{status}: {message[:500]}"
            if reasons:
                safe += f" ({', '.join(reasons[:3])})"
        except (ValueError, TypeError, AttributeError):
            safe = "request rejected without a JSON error body"
        return safe.replace(self.api_key, "[REDACTED]")
