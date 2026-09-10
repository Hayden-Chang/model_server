from dataclasses import dataclass
from typing import Any

import httpx

from .pipelines import Pipeline
from .settings import Settings


class ModelGatewayUnavailable(Exception):
    pass


class ModelGatewayResponseError(Exception):
    pass


@dataclass(frozen=True)
class ModelOutput:
    content: str
    provider_model: str | None
    usage: dict[str, Any] | None


class LiteLLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def is_ready(self) -> bool:
        url = str(self._settings.litellm_base_url).rstrip("/") + "/health/liveliness"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    url,
                    headers=self._authorization_header(),
                )
            return response.is_success
        except httpx.HTTPError:
            return False

    async def complete(self, pipeline: Pipeline, user_input: str) -> ModelOutput:
        payload: dict[str, Any] = {
            "model": pipeline.model_alias or self._settings.litellm_model_alias,
            "messages": pipeline.messages(user_input),
            "temperature": pipeline.temperature,
            "max_tokens": pipeline.max_tokens,
        }
        if pipeline.thinking_mode is not None:
            payload["thinking"] = {"type": pipeline.thinking_mode}
        if pipeline.reasoning_effort is not None:
            payload["reasoning_effort"] = pipeline.reasoning_effort
        if pipeline.response_schema is not None:
            if self._settings.structured_output_mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": pipeline.pipeline_id.replace("-", "_"),
                        "strict": True,
                        "schema": pipeline.response_schema,
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}

        url = str(self._settings.litellm_base_url).rstrip("/") + "/v1/chat/completions"
        try:
            timeout = min(self._settings.model_timeout_seconds, pipeline.timeout_seconds or self._settings.model_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    headers=self._authorization_header(),
                    json=payload,
                )
        except httpx.TimeoutException as error:
            raise ModelGatewayUnavailable("model gateway request timed out") from error
        except httpx.HTTPError as error:
            raise ModelGatewayUnavailable("model gateway could not be reached") from error

        if response.status_code >= 500:
            raise ModelGatewayUnavailable("model gateway is unavailable")
        if not response.is_success:
            raise ModelGatewayResponseError("model gateway rejected the request")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                finish = body["choices"][0].get("finish_reason")
                if finish not in ("stop", "length", "content_filter", "tool_calls"):
                    finish = "unknown"
                raise ModelGatewayResponseError(f"model gateway returned empty content (finish_reason={finish})")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelGatewayResponseError("model gateway returned an invalid response") from error

        usage = body.get("usage")
        return ModelOutput(
            content=content,
            provider_model=body.get("model"),
            usage=usage if isinstance(usage, dict) else None,
        )

    def _authorization_header(self) -> dict[str, str]:
        key = self._settings.litellm_master_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"}
