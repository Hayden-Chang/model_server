import asyncio
import pytest

from app import time_fragment_service as service
from app.contracts import TimeFragmentPlanRequestV2
from app.model_client import ModelGatewayUnavailable
from app.observability import TrackedModelClient
from test_time_fragment_api import FakeModelClient, operations_output, request_payload, settings


def request():
    return TimeFragmentPlanRequestV2.model_validate(request_payload(
        text="看书", date="2026-09-11", now="2026-09-08T12:00:00+08:00",
    ))


@pytest.mark.parametrize("phase", ["initial", "correction"])
def test_each_model_phase_has_a_wall_clock_deadline_and_cancels_pending_work(monkeypatch, phase):
    monkeypatch.setattr(service, "_INITIAL_MODEL_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(service, "_CORRECTION_TIMEOUT_SECONDS", 0.01, raising=False)
    cancelled = []
    calls = []

    class WaitingModel:
        async def complete(self, pipeline, user_input):
            calls.append(pipeline)
            if phase == "correction" and len(calls) == 1:
                return operations_output([{"type": "unknown"}])
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    async def run():
        try:
            await asyncio.wait_for(service.execute_time_fragment_plan(
                WaitingModel(), request(), max_input_chars=100000,
            ), timeout=0.2)
        except ModelGatewayUnavailable as error:
            assert "timed out" in str(error)
        except TimeoutError:
            pytest.fail("planner did not enforce its model phase deadline")
        else:
            pytest.fail("waiting model unexpectedly returned a successful plan")

    asyncio.run(run())
    assert len(calls) == (1 if phase == "initial" else 2)
    assert cancelled == [True]


def test_entire_request_uses_one_deadline_across_both_calls(monkeypatch):
    monkeypatch.setattr(service, "_PLAN_TIMEOUT_SECONDS", 0.04, raising=False)
    monkeypatch.setattr(service, "_INITIAL_MODEL_TIMEOUT_SECONDS", 1.0, raising=False)
    monkeypatch.setattr(service, "_CORRECTION_TIMEOUT_SECONDS", 1.0, raising=False)

    class WaitingCorrection:
        calls = 0

        async def complete(self, pipeline, user_input):
            self.calls += 1
            await asyncio.sleep(0.025)
            if self.calls == 1:
                return operations_output([{"type": "unknown"}])
            return operations_output([])

    model = WaitingCorrection()

    async def run():
        try:
            await asyncio.wait_for(service.execute_time_fragment_plan(
                model, request(), max_input_chars=100000,
            ), timeout=0.2)
        except ModelGatewayUnavailable:
            pass
        except TimeoutError:
            pytest.fail("correction did not share the overall planning deadline")
        else:
            pytest.fail("each call finished within its own limit but exceeded the shared deadline")

    asyncio.run(run())
    assert model.calls == 2


def test_normal_correction_keeps_one_retry_with_explicit_phase_budgets():
    fake = FakeModelClient([operations_output([{"type": "unknown"}]), operations_output([])])
    response = asyncio.run(service.execute_time_fragment_plan(fake, request(), max_input_chars=100000))
    assert response.validation.attempts == 2
    assert [pipeline.timeout_seconds for pipeline, _ in fake.calls] == [30.0, 15.0]
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["enabled", "enabled"]
    assert [pipeline.reasoning_effort for pipeline, _ in fake.calls] == ["high", "high"]


def test_external_cancellation_propagates_without_turning_into_a_retry():
    class CancelledModel:
        async def complete(self, pipeline, user_input):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.execute_time_fragment_plan(CancelledModel(), request(), max_input_chars=100000))


def test_deadline_preserves_the_cancelled_model_call_in_observability(monkeypatch):
    monkeypatch.setattr(service, "_INITIAL_MODEL_TIMEOUT_SECONDS", 0.01)

    class WaitingModel:
        async def complete(self, pipeline, user_input):
            await asyncio.Event().wait()

    tracked = TrackedModelClient(WaitingModel())
    with pytest.raises(ModelGatewayUnavailable):
        asyncio.run(service.execute_time_fragment_plan(tracked, request(), max_input_chars=100000))
    assert len(tracked.calls) == 1
    assert tracked.calls[0].error_type == "CancelledError"
    assert tracked.calls[0].usage_complete is False


def test_deadline_returns_structured_failure_and_refunds_the_quota(monkeypatch, settings):
    from fastapi.testclient import TestClient
    from app.factory import create_app
    from test_time_fragment_api import guest_headers

    monkeypatch.setattr(service, "_INITIAL_MODEL_TIMEOUT_SECONDS", 0.01)

    class OnceWaitingModel:
        calls = 0

        async def complete(self, pipeline, user_input):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return operations_output([])

    model = OnceWaitingModel()
    with TestClient(create_app(settings.model_copy(update={"time_fragment_guest_quota_limit": 1}), model)) as client:
        headers = guest_headers(client, "planning-deadline-regression")
        failure = client.post("/api/plan/parse", headers=headers, json=request().model_dump(mode="json", by_alias=True))
        assert failure.status_code == 503
        assert failure.json()["detail"]["code"] == "MODEL_GATEWAY_UNAVAILABLE"
        retry = request().model_copy(update={"request_id": "retry-after-deadline"})
        response = client.post("/api/plan/parse", headers=headers, json=retry.model_dump(mode="json", by_alias=True))
        assert response.status_code == 200
    assert model.calls == 2
