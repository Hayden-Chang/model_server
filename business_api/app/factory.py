import json
import re
import secrets
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .contracts import (
    ModelMetadata,
    RunRequest,
    RunResponse,
    TimeFragmentGuestRequest,
    TimeFragmentGuestResponse,
    TimeFragmentPlanRequest,
    TimeFragmentPlanResponse,
)
from .guest_auth import GuestTokenCodec, GuestTokenError
from .model_client import (
    LiteLLMClient,
    ModelGatewayResponseError,
    ModelGatewayUnavailable,
    ModelOutput,
)
from .pipelines import get_pipeline
from .postprocessors import ModelOutputInvalid, process_structured, process_text
from .settings import Settings
from .time_fragment import validate_plan_for_request


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def create_app(settings: Settings, model_client: Any | None = None) -> FastAPI:
    app = FastAPI(title="Model Server Business API", version="1.0.0")
    client = model_client or LiteLLMClient(settings)
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
        return response

    async def require_api_key(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.business_api_key.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "invalid bearer token"},
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
            output = await client.complete(pipeline, user_input)
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

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        is_ready = await client.is_ready()
        status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=status_code, content={"status": "ready" if is_ready else "not_ready"})

    @app.post("/api/auth/guest", response_model=TimeFragmentGuestResponse)
    async def time_fragment_guest(payload: TimeFragmentGuestRequest) -> TimeFragmentGuestResponse:
        return TimeFragmentGuestResponse(
            access_token=guest_tokens.issue(payload.device_id),
            expires_in=settings.time_fragment_token_ttl_seconds,
        )

    @app.post(
        "/api/plan/parse",
        response_model=TimeFragmentPlanResponse,
        dependencies=[Depends(require_time_fragment_guest)],
    )
    async def time_fragment_plan_parse(
        payload: TimeFragmentPlanRequest,
    ) -> TimeFragmentPlanResponse:
        user_input = json.dumps(
            payload.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result, _ = await complete_pipeline("time-fragment-plan-v1", user_input)
        try:
            plan = TimeFragmentPlanResponse.model_validate(result)
            validate_plan_for_request(plan, payload.now)
        except (ValidationError, ModelOutputInvalid) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MODEL_OUTPUT_INVALID", "message": str(error)},
            ) from error
        return plan

    @app.post(
        "/v1/pipelines/{pipeline_id}:run",
        response_model=RunResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def run_pipeline(pipeline_id: str, payload: RunRequest, request: Request) -> RunResponse:
        pipeline = get_pipeline(pipeline_id)
        result, output = await complete_pipeline(pipeline_id, payload.input)
        assert pipeline is not None

        return RunResponse(
            pipeline=pipeline.pipeline_id,
            request_id=request.state.request_id,
            result=result,
            model=ModelMetadata(
                alias=settings.litellm_model_alias,
                provider_model=output.provider_model,
                usage=output.usage,
            ),
        )

    return app


def _guest_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "invalid guest bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )
