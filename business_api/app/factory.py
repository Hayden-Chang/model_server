import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from .admin_dashboard import ADMIN_DASHBOARD_HEADERS, ADMIN_DASHBOARD_HTML
from .contracts import (
    ModelMetadata,
    RunRequest,
    RunResponse,
    TimeFragmentGuestRequest,
    TimeFragmentGuestResponse,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanResponseV2,
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
from .pipelines import get_pipeline
from .postprocessors import ModelOutputInvalid, process_structured, process_text
from .settings import Settings
from .time_fragment_service import TimeFragmentInputTooLarge, execute_time_fragment_plan
from .usage_store import InferenceCapture, UsageStore


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
DEVICE_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"
LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    model_client: Any | None = None,
    usage_store: UsageStore | None = None,
) -> FastAPI:
    client = model_client or LiteLLMClient(settings)
    store = usage_store or UsageStore(
        settings.usage_db_path,
        settings.usage_content_retention_days,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        store.close()

    app = FastAPI(title="Model Server Business API", version="1.0.0", lifespan=lifespan)
    guest_tokens = GuestTokenCodec(
        settings.time_fragment_token_secret.get_secret_value(),
        settings.time_fragment_token_ttl_seconds,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if request.url.path.startswith("/admin/observability"):
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
    ) -> tuple[str | dict[str, Any], ModelOutput]:
        pipeline = get_pipeline(pipeline_id)
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
                return process_text(output.content), output
            return process_structured(output.content, pipeline.response_schema), output
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

    @app.post("/api/auth/guest", response_model=TimeFragmentGuestResponse)
    async def time_fragment_guest(payload: TimeFragmentGuestRequest) -> TimeFragmentGuestResponse:
        return TimeFragmentGuestResponse(
            access_token=guest_tokens.issue(payload.device_id),
            expires_in=settings.time_fragment_token_ttl_seconds,
        )

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
        request_content = payload.model_dump(mode="json", by_alias=True)
        try:
            response = await execute_time_fragment_plan(
                tracker,
                payload,
                max_input_chars=settings.max_input_chars,
            )
        except TimeFragmentInputTooLarge as error:
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
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        tracker = TrackedModelClient(client)
        request_device_key = (
            "unattributed" if installation_id is None else guest_tokens.device_key(installation_id)
        )
        pipeline = get_pipeline(pipeline_id)
        try:
            result, output = await complete_pipeline(pipeline_id, payload.input, tracker)
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
        assert pipeline is not None

        response = RunResponse(
            pipeline=pipeline.pipeline_id,
            request_id=request.state.request_id,
            result=result,
            model=ModelMetadata(
                alias=settings.litellm_model_alias,
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

    return app


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
