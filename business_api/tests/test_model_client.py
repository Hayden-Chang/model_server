import asyncio

import httpx
import pytest

from app.model_client import LiteLLMClient
from app.pipelines import PIPELINES
from app.settings import Settings


def make_settings(mode: str) -> Settings:
    return Settings(
        business_api_key="business-test-key-with-32-characters",
        litellm_master_key="litellm-test-key-with-32-characters",
        litellm_base_url="http://litellm:4000",
        structured_output_mode=mode,
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

