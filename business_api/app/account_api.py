"""Public DayMosaic API. Identity/quota are resolved before private planning."""

import json
import logging
import secrets
import re
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .account_backend import AccountAPISettings, AccountBackend, Actor, failure, support_code
from .contracts import (DevelopmentMembershipRequest, DevelopmentMembershipResponse,
                        TimeFragmentGuestRequest, TimeFragmentGuestResponse,
                        TimeFragmentPlanRequestV2, TimeFragmentPlanResponseV2,
                        TimeFragmentQuotaStatusResponse, TimeFragmentQuotaResetAllResponse)
from .guest_auth import GuestTokenCodec, GuestTokenError
from .planning_auth import body_hash

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
LOGGER = logging.getLogger(__name__)


def log_http_failure(request: Request, status: int, detail) -> None:
    code = detail.get("code") if isinstance(detail, dict) else None
    record = {"event": "account_api_failure", "requestID": request.state.request_id,
              "method": request.method,
              "route": getattr(request.scope.get("route"), "path", request.url.path),
              "status": status,
              "code": code if isinstance(code, str) and ERROR_CODE_PATTERN.fullmatch(code) else "HTTP_ERROR"}
    LOGGER.warning(json.dumps(record, separators=(",", ":")))


class ClaimGuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_token: str = Field(min_length=1, max_length=4096)


def create_account_api(settings: AccountAPISettings, backend=None) -> FastAPI:
    backend = backend or AccountBackend(settings)
    tokens = GuestTokenCodec(settings.time_fragment_token_secret.get_secret_value(), settings.time_fragment_token_ttl_seconds)
    development = frozenset(tokens.device_key(value.strip())
                            for value in settings.time_fragment_development_device_ids.split(",") if value.strip())
    development_support = frozenset(support_code(value) for value in development)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await backend.close()

    app = FastAPI(title="DayMosaic Account API", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def logged_http_exception(request: Request, error: HTTPException):
        log_http_failure(request, error.status_code, error.detail)
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail}, headers=error.headers)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-request-id"] = request_id
        return response

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 8192:
            raise failure("UNAUTHORIZED", 401)
        return authorization[7:]

    async def actor(authorization: str | None = Header(default=None)) -> Actor:
        token = bearer(authorization)
        if token.count(".") == 1:
            try:
                return Actor(tokens.verify(token))
            except GuestTokenError as error:
                raise failure("UNAUTHORIZED", 401) from error
        return await backend.account(token)

    async def account(authorization: str | None = Header(default=None)) -> Actor:
        return await backend.account(bearer(authorization))

    async def admin(authorization: str | None = Header(default=None)) -> None:
        supplied = bearer(authorization)
        if not supplied.isascii() or not secrets.compare_digest(supplied, settings.admin_api_key.get_secret_value()):
            raise failure("UNAUTHORIZED", 401)

    async def developer(current: Actor = Depends(actor)) -> Actor:
        if current.principal not in development:
            raise failure("DEVELOPMENT_MEMBERSHIP_DISABLED", 403)
        return current

    async def quota(action: str, current: Actor, diagnostic_request_id: str | None = None, **data):
        return await backend.quota(action, current, diagnostic_request_id=diagnostic_request_id,
                                   developmentAllowed=current.principal in development, **data)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        await backend.quota("ready")
        return {"status": "ready"}

    @app.post("/api/auth/guest", response_model=TimeFragmentGuestResponse)
    async def guest(payload: TimeFragmentGuestRequest):
        await quota("status", Actor(tokens.device_key(payload.device_id)))
        return TimeFragmentGuestResponse(access_token=tokens.issue(payload.device_id), expires_in=settings.time_fragment_token_ttl_seconds)

    @app.get("/api/account/quota", response_model=TimeFragmentQuotaStatusResponse)
    async def status(current: Actor = Depends(actor)):
        return await quota("status", current)

    @app.post("/api/account/claim-guest", response_model=TimeFragmentQuotaStatusResponse)
    async def claim(payload: ClaimGuestRequest, current: Actor = Depends(account)):
        try:
            guest_id = tokens.verify(payload.guest_token)
        except GuestTokenError as error:
            raise failure("UNAUTHORIZED", 401) from error
        # Existing pre-cutover tokens may never have made a charged request.
        # Import is already complete before a new zero-use guest can be created.
        return await quota("claim", current, guest=guest_id, guestSupportCode=support_code(guest_id))

    @app.get("/api/development/membership", response_model=DevelopmentMembershipResponse)
    async def membership(current: Actor = Depends(developer)):
        return await quota("status", current)

    @app.post("/api/development/membership", response_model=DevelopmentMembershipResponse)
    async def toggle(payload: DevelopmentMembershipRequest, current: Actor = Depends(developer)):
        return await quota("membership", current, enabled=payload.enabled)

    @app.post("/api/plan/parse", response_model=TimeFragmentPlanResponseV2)
    async def plan(
        payload: TimeFragmentPlanRequestV2,
        request: Request,
        current: Actor = Depends(actor),
    ):
        body = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        attempt = str(uuid4())
        await quota("reserve", current, diagnostic_request_id=request.state.request_id,
                    requestID=payload.request_id, bodyHash=body_hash(body), attempt=attempt)
        try:
            response = TimeFragmentPlanResponseV2.model_validate(
                await backend.plan(current, body, attempt, request.state.request_id)
            )
            if response.request_id != payload.request_id:
                raise ValueError("response request mismatch")
        except Exception:
            await quota("finish", current, diagnostic_request_id=request.state.request_id,
                        requestID=payload.request_id, attempt=attempt, consume=False)
            raise
        await quota("finish", current, diagnostic_request_id=request.state.request_id,
                    requestID=payload.request_id, attempt=attempt, consume=response.proposal is not None)
        return response

    @app.exception_handler(ValidationError)
    @app.exception_handler(ValueError)
    async def invalid_upstream(request: Request, error):
        log_http_failure(request, 502, {"code": "MODEL_GATEWAY_ERROR"})
        return JSONResponse(status_code=502, content={"detail": {"code": "MODEL_GATEWAY_ERROR", "message": "invalid planning response"}})

    @app.get("/admin/time-fragment/quotas/{code}", response_model=TimeFragmentQuotaStatusResponse, dependencies=[Depends(admin)])
    async def admin_status(code: str):
        return await backend.quota("admin_status", supportCode=code, developmentAllowed=code in development_support)

    @app.post("/admin/time-fragment/quotas/{code}/reset", response_model=TimeFragmentQuotaStatusResponse, dependencies=[Depends(admin)])
    async def admin_reset(code: str):
        return await backend.quota("admin_reset", supportCode=code, developmentAllowed=code in development_support)

    @app.post("/admin/time-fragment/quotas/reset-all", response_model=TimeFragmentQuotaResetAllResponse, dependencies=[Depends(admin)])
    async def admin_reset_all():
        return await backend.quota("admin_reset_all")

    return app
