import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from .admin_dashboard import ADMIN_DASHBOARD_HEADERS, ADMIN_DASHBOARD_HTML
from .contracts import (
    DevelopmentMembershipRequest,
    DevelopmentMembershipResponse,
    ModelMetadata,
    PipelineRuntimeConfigResponse,
    PipelineRuntimeHistoryResponse,
    PipelineRuntimeRollbackRequest,
    PipelineRuntimeUpdateRequest,
    RunRequest,
    RunResponse,
    TimeFragmentGuestRequest,
    TimeFragmentGuestResponse,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanResponseV2,
    TimeFragmentQuotaResetAllResponse,
    TimeFragmentQuotaStatusResponse,
    UsageRecordListResponse,
    UsageSummaryResponse,
)
from .guest_auth import GuestTokenCodec, GuestTokenError
from .model_client import (
    LiteLLMClient,
    ModelGatewayResponseError,
    ModelGatewayUnavailable,
    ModelOutput,
)
from .observability import TrackedModelClient, aggregate_usage
from .pipeline_runtime import (
    PipelineRuntimeConfig,
    PipelineRuntimeInvalidConfig,
    PipelineRuntimeNoPreviousVersion,
    PipelineRuntimeNotConfigurable,
    PipelineRuntimeStore,
    PipelineRuntimeVersionConflict,
)
from .pipelines import Pipeline
from .planning_auth import PlanningCredentials
from .postprocessors import ModelOutputInvalid, process_structured, process_text
from .quota_store import (
    DuplicateRequestCompleted,
    DuplicateRequestInProgress,
    QuotaExceeded,
    QuotaReservation,
    QuotaStatus,
    QuotaStore,
)
from .settings import Settings
from .time_fragment_service import (
    TimeFragmentInputTooLarge,
    TimeFragmentRequestInvalid,
    execute_time_fragment_plan,
)
from .usage_store import InferenceCapture, UsageStore


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
DEVICE_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"
SUPPORT_CODE_PATTERN = r"^TF-[A-Z2-7]{4}-[A-Z2-7]{4}$"
LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    model_client: Any | None = None,
    usage_store: UsageStore | None = None,
    quota_store: QuotaStore | None = None,
    pipeline_runtime_store: PipelineRuntimeStore | None = None,
) -> FastAPI:
    if settings.planning_internal_only and settings.planning_internal_secret is None:
        raise ValueError("Internal planning requires its own credential secret")
    if settings.planning_internal_secret == settings.time_fragment_token_secret:
        raise ValueError("Planning and guest token secrets must be independent")
    client = model_client or LiteLLMClient(settings)
    store = usage_store or UsageStore(
        settings.usage_db_path,
        settings.usage_content_retention_days,
    )
    development_principals = frozenset(
        GuestTokenCodec.device_key(device_id.strip())
        for device_id in settings.time_fragment_development_device_ids.split(",") if device_id.strip()
    )
    quotas = quota_store or QuotaStore(
        settings.usage_db_path,
        settings.time_fragment_guest_quota_limit,
        development_principals=development_principals,
    )
    runtime_pipelines = pipeline_runtime_store or PipelineRuntimeStore(
        settings.usage_db_path,
        settings.litellm_model_alias,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        store.close()
        quotas.close()
        runtime_pipelines.close()

    app = FastAPI(title="Model Server Business API", version="1.0.0", lifespan=lifespan)
    guest_tokens = GuestTokenCodec(
        settings.time_fragment_token_secret.get_secret_value(),
        settings.time_fragment_token_ttl_seconds,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        if settings.planning_internal_only and request.url.path.startswith(("/api/", "/admin/time-fragment/")):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if request.url.path.startswith("/admin/"):
            response.headers["cache-control"] = "no-store"
            response.headers["x-content-type-options"] = "nosniff"
        return response

    async def require_api_key(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.business_api_key.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def require_admin_key(authorization: str | None = Header(default=None)) -> None:
        if settings.admin_api_key is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "ADMIN_API_DISABLED", "message": "admin API is not configured"},
            )
        expected = f"Bearer {settings.admin_api_key.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "invalid admin bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def require_time_fragment_guest(
        authorization: str | None = Header(default=None),
    ) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise _guest_unauthorized()
        try:
            return guest_tokens.verify(authorization.removeprefix("Bearer "))
        except GuestTokenError as error:
            raise _guest_unauthorized() from error

    async def complete_pipeline(
        pipeline_id: str,
        user_input: str,
        model_gateway: Any,
    ) -> tuple[str | dict[str, Any], ModelOutput, Pipeline]:
        pipeline = runtime_pipelines.resolve(pipeline_id)
        if pipeline is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "PIPELINE_NOT_FOUND", "message": "unknown pipeline"},
            )
        if len(user_input) > settings.max_input_chars:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "INPUT_TOO_LARGE", "message": "input exceeds the configured limit"},
            )

        try:
            output = await model_gateway.complete(pipeline, user_input)
            if pipeline.response_schema is None:
                return process_text(output.content), output, pipeline
            return process_structured(output.content, pipeline.response_schema), output, pipeline
        except ModelGatewayUnavailable as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "MODEL_GATEWAY_UNAVAILABLE", "message": str(error)},
            ) from error
        except ModelGatewayResponseError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MODEL_GATEWAY_ERROR", "message": str(error)},
            ) from error
        except ModelOutputInvalid as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MODEL_OUTPUT_INVALID", "message": str(error)},
            ) from error

    def persist_inference(
        *,
        request_id: str,
        device_key: str,
        route: str,
        pipeline: str,
        started_at: datetime,
        started_clock: float,
        status_code: int,
        request_content: Any,
        response_content: Any | None,
        tracker: TrackedModelClient,
    ) -> None:
        completed_at = datetime.now(timezone.utc)
        usage, usage_complete = aggregate_usage(tracker.calls)
        try:
            store.record(
                InferenceCapture(
                    request_id=request_id,
                    device_key=device_key,
                    route=route,
                    pipeline=pipeline,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=max(0, round((time.perf_counter() - started_clock) * 1_000)),
                    status_code=status_code,
                    request_content=request_content,
                    response_content=response_content,
                    model_calls=tracker.calls,
                    usage=usage,
                    usage_complete=usage_complete,
                )
            )
        except Exception:
            LOGGER.exception("failed to persist inference observability record")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        is_ready = await client.is_ready()
        status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=status_code, content={"status": "ready" if is_ready else "not_ready"})

    @app.get("/admin/observability", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/observability/ui", response_class=HTMLResponse, include_in_schema=False)
    async def observability_dashboard() -> HTMLResponse:
        return HTMLResponse(ADMIN_DASHBOARD_HTML, headers=ADMIN_DASHBOARD_HEADERS)

    @app.get(
        "/admin/runtime/pipelines/{pipeline_id}",
        response_model=PipelineRuntimeConfigResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def get_pipeline_runtime_config(pipeline_id: str) -> PipelineRuntimeConfigResponse:
        try:
            return _pipeline_runtime_response(runtime_pipelines.get(pipeline_id))
        except PipelineRuntimeNotConfigurable as error:
            raise _pipeline_runtime_not_configurable() from error

    @app.put(
        "/admin/runtime/pipelines/{pipeline_id}",
        response_model=PipelineRuntimeConfigResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def update_pipeline_runtime_config(
        pipeline_id: str,
        payload: PipelineRuntimeUpdateRequest,
    ) -> PipelineRuntimeConfigResponse:
        try:
            config = runtime_pipelines.update(
                pipeline_id,
                model_alias=payload.model_alias,
                thinking_mode=payload.thinking_mode,
                reasoning_effort=payload.reasoning_effort,
                expected_version=payload.expected_version,
            )
        except PipelineRuntimeNotConfigurable as error:
            raise _pipeline_runtime_not_configurable() from error
        except PipelineRuntimeVersionConflict as error:
            raise _pipeline_runtime_version_conflict(error) from error
        except PipelineRuntimeInvalidConfig as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "PIPELINE_RUNTIME_INVALID", "message": str(error)},
            ) from error
        return _pipeline_runtime_response(config)

    @app.post(
        "/admin/runtime/pipelines/{pipeline_id}/rollback",
        response_model=PipelineRuntimeConfigResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def rollback_pipeline_runtime_config(
        pipeline_id: str,
        payload: PipelineRuntimeRollbackRequest,
    ) -> PipelineRuntimeConfigResponse:
        try:
            config = runtime_pipelines.rollback(
                pipeline_id,
                expected_version=payload.expected_version,
            )
        except PipelineRuntimeNotConfigurable as error:
            raise _pipeline_runtime_not_configurable() from error
        except PipelineRuntimeVersionConflict as error:
            raise _pipeline_runtime_version_conflict(error) from error
        except PipelineRuntimeNoPreviousVersion as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "PIPELINE_RUNTIME_NO_PREVIOUS_VERSION", "message": str(error)},
            ) from error
        return _pipeline_runtime_response(config)

    @app.get(
        "/admin/runtime/pipelines/{pipeline_id}/history",
        response_model=PipelineRuntimeHistoryResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def list_pipeline_runtime_history(pipeline_id: str) -> PipelineRuntimeHistoryResponse:
        try:
            records = runtime_pipelines.history(pipeline_id)
        except PipelineRuntimeNotConfigurable as error:
            raise _pipeline_runtime_not_configurable() from error
        return PipelineRuntimeHistoryResponse(
            records=[_pipeline_runtime_response(record) for record in records]
        )

    @app.post("/api/auth/guest", response_model=TimeFragmentGuestResponse)
    async def time_fragment_guest(payload: TimeFragmentGuestRequest) -> TimeFragmentGuestResponse:
        return TimeFragmentGuestResponse(
            access_token=guest_tokens.issue(payload.device_id),
            expires_in=settings.time_fragment_token_ttl_seconds,
        )

    async def require_development_installation(
        principal: str = Depends(require_time_fragment_guest),
    ) -> str:
        if principal not in development_principals:
            raise HTTPException(status_code=403, detail={
                "code": "DEVELOPMENT_MEMBERSHIP_DISABLED",
                "message": "此安装尚未获准测试会员权益。",
            })
        return principal

    @app.get("/api/development/membership", response_model=DevelopmentMembershipResponse)
    async def development_membership_status(
        principal: str = Depends(require_development_installation),
    ) -> dict[str, object]:
        return quotas.membership_status(principal)

    @app.post("/api/development/membership", response_model=DevelopmentMembershipResponse)
    async def development_membership_update(
        payload: DevelopmentMembershipRequest,
        principal: str = Depends(require_development_installation),
    ) -> dict[str, object]:
        quotas.set_membership(principal, payload.enabled)
        return quotas.membership_status(principal)

    @app.post(
        "/api/plan/parse",
        response_model=TimeFragmentPlanResponseV2,
    )
    async def time_fragment_plan_parse(
        payload: TimeFragmentPlanRequestV2,
        request: Request,
        device_key: str = Depends(require_time_fragment_guest),
    ) -> TimeFragmentPlanResponseV2:
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        tracker = TrackedModelClient(client)
        request_content = payload.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        reservation: QuotaReservation | None = None
        try:
            reservation = quotas.reserve(device_key, payload.request_id)
        except QuotaExceeded as error:
            failure = HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_quota_exhausted_detail(error.quota_status),
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except DuplicateRequestInProgress as error:
            failure = HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "AI_REQUEST_IN_PROGRESS",
                    "message": "a request with this requestID is already in progress",
                },
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except DuplicateRequestCompleted as error:
            failure = HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "AI_REQUEST_ALREADY_COMPLETED",
                    "message": "a request with this requestID has already completed",
                },
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        assert reservation is not None
        try:
            pipeline = runtime_pipelines.resolve("time-fragment-plan-v2")
            assert pipeline is not None
            response = await execute_time_fragment_plan(
                tracker,
                payload,
                max_input_chars=settings.max_input_chars,
                pipeline=pipeline,
            )
        except TimeFragmentRequestInvalid as error:
            quotas.refund(reservation)
            failure = HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": error.code, "message": error.message},
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except TimeFragmentInputTooLarge as error:
            quotas.refund(reservation)
            failure = HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "INPUT_TOO_LARGE", "message": "input exceeds the configured limit"},
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except ModelGatewayUnavailable as error:
            quotas.refund(reservation)
            failure = HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "MODEL_GATEWAY_UNAVAILABLE", "message": str(error)},
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except ModelGatewayResponseError as error:
            quotas.refund(reservation)
            failure = HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MODEL_GATEWAY_ERROR", "message": str(error)},
            )
            persist_inference(
                request_id=request.state.request_id,
                device_key=device_key,
                route="/api/plan/parse",
                pipeline="time-fragment-plan-v2",
                started_at=started_at,
                started_clock=started_clock,
                status_code=failure.status_code,
                request_content=request_content,
                response_content={"detail": failure.detail},
                tracker=tracker,
            )
            raise failure from error
        except Exception:
            quotas.refund(reservation)
            raise

        if response.proposal is None:
            quotas.refund(reservation)
        else:
            quotas.consume(reservation)

        persist_inference(
            request_id=request.state.request_id,
            device_key=device_key,
            route="/api/plan/parse",
            pipeline="time-fragment-plan-v2",
            started_at=started_at,
            started_clock=started_clock,
            status_code=status.HTTP_200_OK,
            request_content=request_content,
            response_content=response.model_dump(mode="json", by_alias=True),
            tracker=tracker,
        )
        return response

    @app.post("/internal/time-fragment/plan", response_model=TimeFragmentPlanResponseV2)
    async def internal_plan(
        payload: TimeFragmentPlanRequestV2,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> TimeFragmentPlanResponseV2:
        if settings.planning_internal_secret is None:
            raise HTTPException(503, detail={"code": "INTERNAL_PLANNING_DISABLED"})
        body = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            claims = PlanningCredentials(settings.planning_internal_secret.get_secret_value()).verify(
                (authorization or "").removeprefix("Bearer "), body)
        except ValueError as error:
            raise HTTPException(401, detail={"code": "UNAUTHORIZED"}) from error
        tracker = TrackedModelClient(client)
        started_at, started_clock = datetime.now(timezone.utc), time.perf_counter()
        status_code = 200
        try:
            pipeline = runtime_pipelines.resolve("time-fragment-plan-v2")
            assert pipeline is not None
            return await execute_time_fragment_plan(
                tracker,
                payload,
                max_input_chars=settings.max_input_chars,
                pipeline=pipeline,
            )
        except TimeFragmentRequestInvalid as error:
            status_code = 422
            raise HTTPException(422, detail={"code": error.code, "message": error.message}) from error
        except TimeFragmentInputTooLarge as error:
            status_code = 413
            raise HTTPException(413, detail={"code": "INPUT_TOO_LARGE"}) from error
        except ModelGatewayUnavailable as error:
            status_code = 503
            raise HTTPException(503, detail={"code": "MODEL_GATEWAY_UNAVAILABLE"}) from error
        except ModelGatewayResponseError as error:
            status_code = 502
            raise HTTPException(502, detail={"code": "MODEL_GATEWAY_ERROR"}) from error
        except Exception:
            status_code = 500
            raise
        finally:
            tracker.calls = [replace(call, input_content="", output_content=None, error_message=None)
                             for call in tracker.calls]
            persist_inference(request_id=request.state.request_id, device_key=claims["sub"],
                route="/internal/time-fragment/plan", pipeline="time-fragment-plan-v2",
                started_at=started_at, started_clock=started_clock, status_code=status_code,
                request_content=None, response_content=None, tracker=tracker)

    @app.post(
        "/v1/pipelines/{pipeline_id}:run",
        response_model=RunResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def run_pipeline(
        pipeline_id: str,
        payload: RunRequest,
        request: Request,
        installation_id: Annotated[
            str | None,
            Header(alias="X-Device-ID", min_length=16, max_length=200, pattern=DEVICE_ID_PATTERN),
        ] = None,
    ) -> RunResponse:
        if settings.planning_internal_only and pipeline_id.startswith("time-fragment"):
            raise HTTPException(403, detail={"code": "INTERNAL_PLANNING_REQUIRED"})
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        tracker = TrackedModelClient(client)
        request_device_key = (
            "unattributed" if installation_id is None else guest_tokens.device_key(installation_id)
        )
        try:
            result, output, pipeline = await complete_pipeline(pipeline_id, payload.input, tracker)
        except HTTPException as error:
            persist_inference(
                request_id=request.state.request_id,
                device_key=request_device_key,
                route=f"/v1/pipelines/{pipeline_id}:run",
                pipeline=pipeline_id,
                started_at=started_at,
                started_clock=started_clock,
                status_code=error.status_code,
                request_content=payload.model_dump(mode="json"),
                response_content={"detail": error.detail},
                tracker=tracker,
            )
            raise
        response = RunResponse(
            pipeline=pipeline.pipeline_id,
            request_id=request.state.request_id,
            result=result,
            model=ModelMetadata(
                alias=pipeline.model_alias or settings.litellm_model_alias,
                provider_model=output.provider_model,
                usage=output.usage,
            ),
        )
        persist_inference(
            request_id=request.state.request_id,
            device_key=request_device_key,
            route=f"/v1/pipelines/{pipeline_id}:run",
            pipeline=pipeline_id,
            started_at=started_at,
            started_clock=started_clock,
            status_code=status.HTTP_200_OK,
            request_content=payload.model_dump(mode="json"),
            response_content=response.model_dump(mode="json"),
            tracker=tracker,
        )
        return response

    @app.get(
        "/admin/observability/requests",
        response_model=UsageRecordListResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def list_observability_requests(
        device_id: Annotated[
            str | None,
            Query(min_length=16, max_length=200, pattern=DEVICE_ID_PATTERN),
        ] = None,
        device_key: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> UsageRecordListResponse:
        resolved_device = _resolve_device_filter(guest_tokens, device_id, device_key)
        _validate_time_range(start_time, end_time)
        records, total = store.list_requests(
            device_key=resolved_device,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )
        return UsageRecordListResponse(total=total, limit=limit, offset=offset, records=records)

    @app.get(
        "/admin/observability/summary",
        response_model=UsageSummaryResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def summarize_observability(
        device_id: Annotated[
            str | None,
            Query(min_length=16, max_length=200, pattern=DEVICE_ID_PATTERN),
        ] = None,
        device_key: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> UsageSummaryResponse:
        resolved_device = _resolve_device_filter(guest_tokens, device_id, device_key)
        _validate_time_range(start_time, end_time)
        totals, devices = store.summarize(
            device_key=resolved_device,
            start_time=start_time,
            end_time=end_time,
        )
        return UsageSummaryResponse(
            device_key=resolved_device,
            start_time=start_time,
            end_time=end_time,
            totals=totals,
            devices=devices,
        )

    @app.get(
        "/admin/time-fragment/quotas/{support_code}",
        response_model=TimeFragmentQuotaStatusResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def get_time_fragment_quota(
        support_code: Annotated[str, Path(pattern=SUPPORT_CODE_PATTERN)],
    ) -> TimeFragmentQuotaStatusResponse:
        quota_status = quotas.status(support_code)
        if quota_status is None:
            raise _unknown_support_code()
        return _quota_status_response(quota_status)

    @app.post(
        "/admin/time-fragment/quotas/{support_code}/reset",
        response_model=TimeFragmentQuotaStatusResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def reset_time_fragment_quota(
        support_code: Annotated[str, Path(pattern=SUPPORT_CODE_PATTERN)],
    ) -> TimeFragmentQuotaStatusResponse:
        quota_status = quotas.reset(support_code)
        if quota_status is None:
            raise _unknown_support_code()
        return _quota_status_response(quota_status)

    @app.post(
        "/admin/time-fragment/quotas/reset-all",
        response_model=TimeFragmentQuotaResetAllResponse,
        dependencies=[Depends(require_admin_key)],
    )
    async def reset_all_time_fragment_quotas() -> TimeFragmentQuotaResetAllResponse:
        return TimeFragmentQuotaResetAllResponse(
            refreshed_installations=quotas.reset_all()
        )

    return app


def _pipeline_runtime_response(config: PipelineRuntimeConfig) -> PipelineRuntimeConfigResponse:
    return PipelineRuntimeConfigResponse(
        pipeline_id=config.pipeline_id,
        model_alias=config.model_alias,
        thinking_mode=config.thinking_mode,
        reasoning_effort=config.reasoning_effort,
        version=config.version,
        source=config.source,
        updated_at=config.updated_at,
    )


def _pipeline_runtime_not_configurable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "PIPELINE_RUNTIME_NOT_CONFIGURABLE",
            "message": "pipeline does not support runtime configuration",
        },
    )


def _pipeline_runtime_version_conflict(error: PipelineRuntimeVersionConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "PIPELINE_RUNTIME_VERSION_CONFLICT",
            "message": str(error),
            "expectedVersion": error.expected,
            "actualVersion": error.actual,
        },
    )


def _resolve_device_filter(
    guest_tokens: GuestTokenCodec,
    device_id: str | None,
    device_key: str | None,
) -> str | None:
    if device_id is not None and device_key is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "AMBIGUOUS_DEVICE_FILTER",
                "message": "provide either device_id or device_key, not both",
            },
        )
    return guest_tokens.device_key(device_id) if device_id is not None else device_key


def _quota_exhausted_detail(quota_status: QuotaStatus) -> dict[str, Any]:
    if quota_status.resets_at is not None:
        return {
            "code": "AI_DAILY_QUOTA_EXHAUSTED",
            "message": f"今日的 50 次 AI 排程额度已用完，将于北京时间 {quota_status.resets_at[:10]} 00:00 恢复。",
            "limit": quota_status.quota_limit,
            "remaining": 0,
            "resetsAt": quota_status.resets_at,
        }
    return {
        "code": "AI_QUOTA_EXHAUSTED",
        "message": "本轮内测的 AI 额度已用完，请将支持码发给开发者刷新。",
        "limit": quota_status.quota_limit,
        "remaining": quota_status.remaining,
        "supportCode": quota_status.support_code,
    }


def _quota_status_response(quota_status: QuotaStatus) -> TimeFragmentQuotaStatusResponse:
    return TimeFragmentQuotaStatusResponse(
        support_code=quota_status.support_code,
        limit=quota_status.quota_limit,
        used=quota_status.used,
        remaining=quota_status.remaining,
    )


def _unknown_support_code() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "SUPPORT_CODE_NOT_FOUND", "message": "unknown support code"},
    )


def _validate_time_range(start_time: datetime | None, end_time: datetime | None) -> None:
    for value in (start_time, end_time):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_TIME_RANGE", "message": "timestamps must include timezone"},
            )
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_TIME_RANGE", "message": "start_time must be before end_time"},
        )


def _guest_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "invalid guest bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )
