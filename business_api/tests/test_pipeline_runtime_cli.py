import importlib.util
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "set-pipeline-runtime.py"
SPEC = importlib.util.spec_from_file_location("set_pipeline_runtime", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def config(*, version: int, thinking: str, effort: str | None) -> dict[str, Any]:
    return {
        "pipelineId": "time-fragment-plan-v2",
        "modelAlias": "primary-model",
        "thinkingMode": thinking,
        "reasoningEffort": effort,
        "version": version,
        "source": "default" if version == 0 else "override",
        "updatedAt": None,
    }


def test_switch_updates_then_runs_probe() -> None:
    calls: list[dict[str, Any]] = []

    def request(base_url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"base_url": base_url, "path": path, **kwargs})
        return config(version=0, thinking="disabled", effort=None) if len(calls) == 1 else {
            **config(version=1, thinking="enabled", effort="low"),
            "modelAlias": "deepseek-flash",
        }

    probes: list[str] = []
    result = MODULE.switch_pipeline(
        base_url="https://example.test",
        admin_key="secret",
        pipeline_id="time-fragment-plan-v2",
        model_alias="deepseek-flash",
        thinking_mode="enabled",
        reasoning_effort="low",
        run_probe=True,
        request=request,
        probe=probes.append,
    )

    assert result["changed"] is True and result["probed"] is True
    assert calls[1]["method"] == "PUT"
    assert calls[1]["payload"] == {
        "modelAlias": "deepseek-flash",
        "thinkingMode": "enabled",
        "reasoningEffort": "low",
        "expectedVersion": 0,
    }
    assert probes == ["https://example.test"]


def test_probe_failure_restores_previous_configuration() -> None:
    calls: list[dict[str, Any]] = []
    responses = iter([
        config(version=4, thinking="disabled", effort=None),
        config(version=5, thinking="enabled", effort="high"),
        config(version=6, thinking="disabled", effort=None),
    ])

    def request(base_url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"path": path, **kwargs})
        return next(responses)

    def failing_probe(_: str) -> None:
        raise RuntimeError("upstream rejected setting")

    with pytest.raises(MODULE.RuntimeSwitchError, match="restored.*version 6"):
        MODULE.switch_pipeline(
            base_url="https://example.test",
            admin_key="secret",
            pipeline_id="time-fragment-plan-v2",
            model_alias=None,
            thinking_mode="enabled",
            reasoning_effort="high",
            run_probe=True,
            request=request,
            probe=failing_probe,
        )

    assert calls[2]["payload"] == {
        "modelAlias": "primary-model",
        "thinkingMode": "disabled",
        "reasoningEffort": None,
        "expectedVersion": 5,
    }


def test_unchanged_configuration_skips_write_and_probe() -> None:
    calls: list[dict[str, Any]] = []

    def request(base_url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"path": path, **kwargs})
        return config(version=3, thinking="disabled", effort=None)

    result = MODULE.switch_pipeline(
        base_url="https://example.test",
        admin_key="secret",
        pipeline_id="time-fragment-plan-v2",
        model_alias=None,
        thinking_mode="disabled",
        reasoning_effort=None,
        run_probe=True,
        request=request,
        probe=lambda _: pytest.fail("unchanged configuration must not probe"),
    )

    assert result["changed"] is False and result["probed"] is False
    assert len(calls) == 1
