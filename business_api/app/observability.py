import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .model_client import ModelOutput


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ModelCallCapture:
    call_index: int
    pipeline: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    input_content: str
    output_content: str | None
    provider_model: str | None
    usage: TokenUsage | None
    usage_complete: bool
    error_type: str | None
    error_message: str | None
    request_method: str | None = None
    request_url: str | None = None
    request_headers: dict[str, str] | None = None
    request_body: dict[str, Any] | None = None
    response_status_code: int | None = None
    response_body: dict[str, Any] | None = None


class TrackedModelClient:
    def __init__(self, client: Any, *, capture_http: bool = False) -> None:
        self._client = client
        self._capture_http = capture_http
        self.calls: list[ModelCallCapture] = []

    async def complete(self, pipeline: Any, user_input: str) -> ModelOutput:
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        call_index = len(self.calls) + 1
        try:
            traced_complete = getattr(self._client, "complete_with_http_trace", None)
            if self._capture_http and traced_complete is not None:
                output = await traced_complete(pipeline, user_input)
            else:
                output = await self._client.complete(pipeline, user_input)
        except (Exception, asyncio.CancelledError) as error:
            completed_at = datetime.now(timezone.utc)
            self.calls.append(
                ModelCallCapture(
                    call_index=call_index,
                    pipeline=pipeline.pipeline_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=_duration_ms(started_clock),
                    input_content=user_input,
                    output_content=None,
                    provider_model=None,
                    usage=None,
                    usage_complete=False,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            )
            raise

        completed_at = datetime.now(timezone.utc)
        usage, usage_complete = normalize_usage(output.usage)
        exchange = output.http_exchange if self._capture_http else None
        self.calls.append(
            ModelCallCapture(
                call_index=call_index,
                pipeline=pipeline.pipeline_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=_duration_ms(started_clock),
                input_content=user_input,
                output_content=output.content,
                provider_model=output.provider_model,
                usage=usage,
                usage_complete=usage_complete,
                error_type=None,
                error_message=None,
                request_method=None if exchange is None else exchange.request_method,
                request_url=None if exchange is None else exchange.request_url,
                request_headers=None if exchange is None else exchange.request_headers,
                request_body=None if exchange is None else exchange.request_body,
                response_status_code=None if exchange is None else exchange.response_status_code,
                response_body=None if exchange is None else exchange.response_body,
            )
        )
        return output


def aggregate_usage(calls: list[ModelCallCapture]) -> tuple[TokenUsage | None, bool]:
    reported = [call.usage for call in calls if call.usage is not None]
    if not reported:
        return None, False
    return (
        TokenUsage(
            prompt_tokens=sum(usage.prompt_tokens for usage in reported),
            completion_tokens=sum(usage.completion_tokens for usage in reported),
            total_tokens=sum(usage.total_tokens for usage in reported),
        ),
        len(reported) == len(calls) and all(call.usage_complete for call in calls),
    )


def normalize_usage(usage: Any) -> tuple[TokenUsage | None, bool]:
    if not isinstance(usage, dict):
        return None, False
    prompt = _nonnegative_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _nonnegative_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _nonnegative_int(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    if prompt is None or completion is None or total is None:
        return None, False
    return TokenUsage(prompt, completion, total), True


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _duration_ms(started_clock: float) -> int:
    return max(0, round((time.perf_counter() - started_clock) * 1_000))
