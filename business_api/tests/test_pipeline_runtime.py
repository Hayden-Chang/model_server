from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import pytest
import yaml
from fastapi.testclient import TestClient

from app.factory import create_app
from app.model_client import ModelOutput
from app.pipeline_runtime import PipelineRuntimeStore, PipelineRuntimeVersionConflict
from app.settings import Settings


API_KEY = "business-test-key-with-32-characters"
ADMIN_KEY = "admin-test-key-with-32-characters"


@dataclass
class RecordingModelClient:
    calls: list[tuple[Any, str]] = field(default_factory=list)

    async def is_ready(self) -> bool:
        return True

    async def complete(self, pipeline: Any, user_input: str) -> ModelOutput:
        self.calls.append((pipeline, user_input))
        return ModelOutput(
            content='{"operations":[],"temporalRelations":[]}',
            provider_model="provider/model-a",
            usage={"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        )


def configured_settings(database: Path) -> Settings:
    return Settings(
        business_api_key=API_KEY,
        litellm_master_key="litellm-test-key-with-32-characters",
        litellm_base_url="http://litellm:4000",
        max_input_chars=20_000,
        time_fragment_token_secret="time-fragment-test-token-secret-with-32-characters",
        admin_api_key=ADMIN_KEY,
        usage_db_path=str(database),
    )


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def enabled_payload(*, expected_version: int = 0) -> dict[str, Any]:
    return {
        "modelAlias": "deepseek-flash",
        "thinkingMode": "enabled",
        "reasoningEffort": "low",
        "expectedVersion": expected_version,
    }


def test_admin_update_applies_immediately_and_persists_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "usage.sqlite3"
    settings = configured_settings(database)
    model = RecordingModelClient()

    with TestClient(create_app(settings, model)) as client:
        initial = client.get(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
        )
        assert initial.status_code == 200
        assert initial.json() == {
            "pipelineId": "time-fragment-plan-v2",
            "modelAlias": "primary-model",
            "thinkingMode": "disabled",
            "reasoningEffort": None,
            "version": 0,
            "source": "default",
            "updatedAt": None,
        }

        updated = client.put(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
            json=enabled_payload(),
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 1

        run = client.post(
            "/v1/pipelines/time-fragment-plan-v2:run",
            headers=api_headers(),
            json={"input": "{}"},
        )
        assert run.status_code == 200
        assert run.json()["model"]["alias"] == "deepseek-flash"
        pipeline, _ = model.calls[-1]
        assert pipeline.model_alias == "deepseek-flash"
        assert pipeline.thinking_mode == "enabled"
        assert pipeline.reasoning_effort == "low"

    with TestClient(create_app(settings, RecordingModelClient())) as restarted:
        persisted = restarted.get(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
        )

    assert persisted.status_code == 200
    assert persisted.json() == {
        "pipelineId": "time-fragment-plan-v2",
        "modelAlias": "deepseek-flash",
        "thinkingMode": "enabled",
        "reasoningEffort": "low",
        "version": 1,
        "source": "override",
        "updatedAt": ANY,
    }


def test_time_fragment_route_uses_the_runtime_snapshot(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path / "usage.sqlite3")
    model = RecordingModelClient()
    with TestClient(create_app(settings, model)) as client:
        updated = client.put(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
            json=enabled_payload(),
        )
        guest = client.post(
            "/api/auth/guest",
            json={"device_id": "runtime-config-route-test"},
        ).json()["access_token"]
        response = client.post(
            "/api/plan/parse",
            headers={"Authorization": f"Bearer {guest}"},
            json={
                "text": "新增一个任务",
                "requestID": "runtime-config-request",
                "baseFingerprint": "sha256:runtime-config-test",
                "currentPlan": {"date": "2026-09-11", "items": []},
                "now": "2026-09-11T00:00:00+08:00",
            },
        )

    assert updated.status_code == 200
    assert response.status_code == 200
    pipeline, _ = model.calls[-1]
    assert pipeline.model_alias == "deepseek-flash"
    assert pipeline.thinking_mode == "enabled"
    assert pipeline.reasoning_effort == "low"


def test_admin_runtime_config_requires_auth_and_only_allows_v2(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path / "usage.sqlite3")
    with TestClient(create_app(settings, RecordingModelClient())) as client:
        unauthorized = client.get("/admin/runtime/pipelines/time-fragment-plan-v2")
        unsupported = client.get(
            "/admin/runtime/pipelines/general-text-v1",
            headers=admin_headers(),
        )

    assert unauthorized.status_code == 401
    assert unsupported.status_code == 404
    assert unsupported.json()["detail"]["code"] == "PIPELINE_RUNTIME_NOT_CONFIGURABLE"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "modelAlias": "primary-model",
            "thinkingMode": "enabled",
            "reasoningEffort": None,
            "expectedVersion": 0,
        },
        {
            "modelAlias": "primary-model",
            "thinkingMode": "disabled",
            "reasoningEffort": "low",
            "expectedVersion": 0,
        },
        {
            "modelAlias": "bad alias with spaces",
            "thinkingMode": "disabled",
            "reasoningEffort": None,
            "expectedVersion": 0,
        },
    ],
)
def test_admin_runtime_config_rejects_inconsistent_reasoning(payload: dict[str, Any], tmp_path: Path) -> None:
    settings = configured_settings(tmp_path / "usage.sqlite3")
    with TestClient(create_app(settings, RecordingModelClient())) as client:
        response = client.put(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
            json=payload,
        )

    assert response.status_code == 422


def test_admin_runtime_config_detects_stale_writes_and_can_rollback(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path / "usage.sqlite3")
    with TestClient(create_app(settings, RecordingModelClient())) as client:
        first = client.put(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
            json=enabled_payload(),
        )
        conflict = client.put(
            "/admin/runtime/pipelines/time-fragment-plan-v2",
            headers=admin_headers(),
            json=enabled_payload(),
        )
        rolled_back = client.post(
            "/admin/runtime/pipelines/time-fragment-plan-v2/rollback",
            headers=admin_headers(),
            json={"expectedVersion": first.json()["version"]},
        )
        history = client.get(
            "/admin/runtime/pipelines/time-fragment-plan-v2/history",
            headers=admin_headers(),
        )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "PIPELINE_RUNTIME_VERSION_CONFLICT"
    assert rolled_back.status_code == 200
    assert rolled_back.json()["thinkingMode"] == "disabled"
    assert rolled_back.json()["reasoningEffort"] is None
    assert rolled_back.json()["version"] == 2
    assert [record["version"] for record in history.json()["records"]] == [2, 1, 0]


def test_runtime_store_noop_does_not_advance_version(tmp_path: Path) -> None:
    store = PipelineRuntimeStore(str(tmp_path / "runtime.sqlite3"), "primary-model")
    try:
        current = store.get("time-fragment-plan-v2")
        unchanged = store.update(
            "time-fragment-plan-v2",
            model_alias=current.model_alias,
            thinking_mode=current.thinking_mode,
            reasoning_effort=current.reasoning_effort,
            expected_version=current.version,
        )
        assert unchanged == current
        with pytest.raises(PipelineRuntimeVersionConflict):
            store.update(
                "time-fragment-plan-v2",
                model_alias="deepseek-flash",
                thinking_mode="enabled",
                reasoning_effort="low",
                expected_version=99,
            )
    finally:
        store.close()


def test_runtime_store_serializes_versions_across_connections(tmp_path: Path) -> None:
    database = str(tmp_path / "runtime.sqlite3")
    first = PipelineRuntimeStore(database, "primary-model")
    second = PipelineRuntimeStore(database, "primary-model")
    try:
        first.update(
            "time-fragment-plan-v2",
            model_alias="primary-model",
            thinking_mode="enabled",
            reasoning_effort="low",
            expected_version=0,
        )
        with pytest.raises(PipelineRuntimeVersionConflict) as conflict:
            second.update(
                "time-fragment-plan-v2",
                model_alias="primary-model",
                thinking_mode="enabled",
                reasoning_effort="high",
                expected_version=0,
            )
        assert conflict.value.actual == 1
    finally:
        first.close()
        second.close()


def test_litellm_preconfigures_the_runtime_deepseek_flash_alias() -> None:
    repository = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((repository / "litellm/config.yaml").read_text(encoding="utf-8"))
    models = {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}

    assert models["deepseek-flash"] == {
        "model": "deepseek/deepseek-flash",
        "api_base": "os.environ/LLM_API_BASE",
        "api_key": "os.environ/LLM_API_KEY",
    }
