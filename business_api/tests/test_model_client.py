import asyncio
from dataclasses import replace

import httpx
import pytest

from app.model_client import LiteLLMClient, ModelGatewayResponseError, ModelGatewayUnavailable
from app.pipelines import PIPELINES
from app.settings import Settings


def make_settings(mode: str) -> Settings:
    return Settings(
        business_api_key="business-test-key-with-32-characters",
        litellm_master_key="litellm-test-key-with-32-characters",
        litellm_base_url="http://litellm:4000",
        structured_output_mode=mode,
        time_fragment_token_secret="time-fragment-test-token-secret-with-32-characters",
    )


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("json_schema", "json_schema"), ("json_object", "json_object")],
)
def test_structured_output_mode_is_applied(monkeypatch: pytest.MonkeyPatch, mode: str, expected_type: str) -> None:
    captured: dict = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        captured.update(kwargs["json"])  # type: ignore[index]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "provider/model-a",
                "choices": [{"message": {"content": '{"summary":"S","key_points":[],"risks":[]}'}}],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    client = LiteLLMClient(make_settings(mode))
    asyncio.run(client.complete(PIPELINES["general-analysis-v1"], "analyze"))

    assert captured["model"] == "primary-model"
    assert captured["response_format"]["type"] == expected_type


def test_pipeline_model_alias_overrides_the_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        captured.update(kwargs["json"])  # type: ignore[index]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"model": "provider/model-a", "choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    pipeline = replace(PIPELINES["general-text-v1"], model_alias="deepseek-flash")

    asyncio.run(LiteLLMClient(make_settings("json_object")).complete(pipeline, "test"))

    assert captured["model"] == "deepseek-flash"


def test_correction_forwards_low_effort_and_bounds_timeout(monkeypatch):
    async def fake_post(self, url, **kwargs):
        assert self.timeout.read == 30.0
        assert kwargs["json"]["reasoning_effort"] == "low"
        assert kwargs["json"]["thinking"] == {"type": "enabled"}
        raise httpx.ReadTimeout("private upstream message")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    pipeline = replace(PIPELINES["time-fragment-plan-v2"], thinking_mode="enabled",
                       reasoning_effort="low", timeout_seconds=30.0)
    with pytest.raises(ModelGatewayUnavailable, match="model gateway request timed out"):
        asyncio.run(LiteLLMClient(make_settings("json_object")).complete(pipeline, "test"))


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_model_content_keeps_safe_finish_reason(monkeypatch, content):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length",
            "message": {"content": content, "reasoning_content": "must not be exposed"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(ModelGatewayResponseError, match="empty content.*finish_reason=length") as error:
        asyncio.run(LiteLLMClient(make_settings("json_object")).complete(PIPELINES["general-text-v1"], "test"))
    assert "must not be exposed" not in str(error.value)
