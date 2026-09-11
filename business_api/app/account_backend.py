import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .planning_auth import PlanningCredentials

LOGGER = logging.getLogger(__name__)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_ERROR_CODES = frozenset({
    "ACCOUNT_SERVICE_UNAVAILABLE", "ACCOUNT_UNAVAILABLE", "AI_ACCOUNT_REQUIRED",
    "AI_DAILY_QUOTA_EXHAUSTED", "AI_QUOTA_EXHAUSTED", "AI_REQUEST_ALREADY_COMPLETED",
    "AI_REQUEST_ID_CONFLICT", "AI_REQUEST_IN_PROGRESS", "DEVELOPMENT_MEMBERSHIP_DISABLED",
    "EARLIEST_START_REQUIRED", "INPUT_TOO_LARGE", "INTERNAL_PLANNING_DISABLED",
    "INVALID_TIME_RANGE", "MODEL_GATEWAY_ERROR", "MODEL_GATEWAY_UNAVAILABLE",
    "PLANNING_DATE_NOT_ALLOWED", "SUPPORT_CODE_NOT_FOUND", "UNAUTHORIZED",
})


def error_class(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.ConnectError):
        return "connection"
    if isinstance(error, httpx.TransportError):
        return "transport"
    if isinstance(error, httpx.HTTPStatusError):
        return "http_status"
    return "invalid_response"


def response_error_code(response: httpx.Response | None) -> str | None:
    try:
        body = response.json() if response is not None else {}
        detail = body.get("detail", {}) if isinstance(body, dict) else {}
        code = detail.get("code") if isinstance(detail, dict) else None
        return code if code in SAFE_ERROR_CODES else None
    except (ValueError, TypeError):
        return None


def log_failure(component: str, operation: str, error: Exception, started: float,
                attempts: int, response: httpx.Response | None = None, request_id: str | None = None) -> None:
    record = {"event": "account_backend_failure", "component": component, "operation": operation,
              "errorClass": error_class(error), "attempts": attempts,
              "durationMs": round((time.perf_counter() - started) * 1000)}
    if response is not None:
        record["upstreamStatus"] = response.status_code
        code = response_error_code(response)
        if code:
            record["upstreamCode"] = code
    if isinstance(request_id, str) and SAFE_REQUEST_ID.fullmatch(request_id):
        record["requestID"] = request_id
    LOGGER.warning(json.dumps(record, separators=(",", ":"), sort_keys=True))


class AccountAPISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: HttpUrl
    supabase_publishable_key: SecretStr
    supabase_service_role_key: SecretStr
    planning_base_url: HttpUrl = "http://business-api:8000"
    planning_internal_secret: SecretStr = Field(min_length=32)
    time_fragment_token_secret: SecretStr = Field(min_length=32)
    time_fragment_token_ttl_seconds: int = Field(default=2592000, ge=300)
    time_fragment_guest_quota_limit: int = Field(default=50, ge=1, le=10000)
    time_fragment_development_device_ids: str = ""
    admin_api_key: SecretStr = Field(min_length=16)
    apple_environment: str = "sandbox"
    apple_key_id: str = ""
    apple_issuer_id: str = ""
    apple_bundle_id: str = "com.hayden.timefragment"
    apple_private_key: SecretStr | None = None
    apple_product_ids: str = "com.hayden.daymosaic.plus.monthly,com.hayden.daymosaic.plus.yearly"
    store_reference_key: SecretStr | None = None

    @model_validator(mode="after")
    def independent_planning_secret(self):
        if self.planning_internal_secret == self.time_fragment_token_secret:
            raise ValueError("Planning and guest token secrets must be independent")
        return self


def failure(code: str, status: int, **details) -> HTTPException:
    messages = {
        "UNAUTHORIZED": "invalid bearer token",
        "ACCOUNT_UNAVAILABLE": "account or session is no longer available",
        "ACCOUNT_SERVICE_UNAVAILABLE": "account service is temporarily unavailable",
        "MODEL_GATEWAY_UNAVAILABLE": "model gateway is temporarily unavailable",
        "AI_ACCOUNT_REQUIRED": "sign in to the account linked to this installation",
        "AI_REQUEST_IN_PROGRESS": "a request with this requestID is already in progress",
        "AI_REQUEST_ALREADY_COMPLETED": "a request with this requestID has already completed",
        "AI_QUOTA_EXHAUSTED": "本轮内测的 AI 额度已用完，请将支持码发给开发者刷新。",
        "AI_DAILY_QUOTA_EXHAUSTED": "今日的 50 次 AI 排程额度已用完，将于北京时间次日 00:00 恢复。",
    }
    return HTTPException(status, detail={"code": code, "message": messages.get(code, "request could not be completed"), **details})


@dataclass(frozen=True)
class Actor:
    principal: str
    session_id: str | None = None


def support_code(principal: str) -> str:
    value = base64.b32encode(hashlib.sha256(("0:" + principal).encode()).digest()[:5]).decode()
    return f"TF-{value[:4]}-{value[4:]}"


class AccountBackend:
    def __init__(self, settings: AccountAPISettings, transport=None) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=15, transport=transport)
        self.credentials = PlanningCredentials(settings.planning_internal_secret.get_secret_value())

    async def close(self) -> None:
        await self.http.aclose()

    async def _rpc(self, name: str, data: dict, token: str | None = None,
                   request_id: str | None = None) -> dict:
        started, attempts, response = time.perf_counter(), 0, None
        key = (self.settings.supabase_publishable_key if token else self.settings.supabase_service_role_key).get_secret_value()
        headers = {"apikey": key, "Authorization": "Bearer " + (token or key)}
        try:
            # Repeating a mutation uses the same attempt ID. No ambiguous request
            # is replaced by a newly generated reservation or a fresh model call.
            for retry in range(2):
                try:
                    attempts += 1
                    response = await self.http.post(str(self.settings.supabase_url).rstrip("/") + "/rest/v1/rpc/" + name,
                                                    headers=headers, json=data)
                    break
                except httpx.TransportError:
                    if retry:
                        raise
            if token and response.status_code in (401, 403):
                raise failure("UNAUTHORIZED", 401)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError) as error:
            action = data.get("p_action") if name == "ai_quota_service" else None
            operation = name + ("." + action if isinstance(action, str) and SAFE_REQUEST_ID.fullmatch(action) else "")
            log_failure("supabase", operation, error, started, attempts, response, request_id)
            raise failure("ACCOUNT_SERVICE_UNAVAILABLE", 503) from error

    async def account(self, token: str) -> Actor:
        result = await self._rpc("ai_account_identity", {}, token)
        try:
            return Actor("account:" + str(UUID(result["userID"])), str(UUID(result["sessionID"])))
        except (KeyError, TypeError, ValueError) as error:
            raise failure("ACCOUNT_SERVICE_UNAVAILABLE", 503) from error

    async def quota(self, action: str, actor: Actor | None = None,
                    diagnostic_request_id: str | None = None, **data) -> dict:
        if actor:
            data.update(principal=actor.principal, sessionID=actor.session_id,
                        supportCode=support_code(actor.principal), freeLimit=self.settings.time_fragment_guest_quota_limit)
        result = await self._rpc("ai_quota_service", {"p_action": action, "p_data": data},
                                 request_id=diagnostic_request_id)
        code = result.pop("code", None)
        if code:
            status = {"AI_QUOTA_EXHAUSTED": 429, "AI_DAILY_QUOTA_EXHAUSTED": 429,
                      "ACCOUNT_UNAVAILABLE": 401, "AI_ACCOUNT_REQUIRED": 401,
                      "DEVELOPMENT_MEMBERSHIP_DISABLED": 403, "SUPPORT_CODE_NOT_FOUND": 404,
                      "ACCOUNT_SERVICE_UNAVAILABLE": 503}.get(code, 409)
            result.pop("period", None)
            raise failure(code, status, **result)
        result.pop("period", None)
        return result

    async def billing(self, action: str, actor: Actor,
                      diagnostic_request_id: str | None = None, **data) -> dict:
        data.update(principal=actor.principal, sessionID=actor.session_id)
        result = await self._rpc("billing_service", {"p_action": action, "p_data": data},
                                 request_id=diagnostic_request_id)
        code = result.pop("code", None)
        if code:
            status = {"CLAIM_CONFLICT": 409, "CLAIM_NOT_FOUND": 404, "ACCOUNT_REQUIRED": 401,
                      "ACCOUNT_UNAVAILABLE": 401, "PRODUCT_INVALID": 422,
                      "ACCOUNT_SERVICE_UNAVAILABLE": 503}.get(code, 409)
            raise failure(code, status, **result)
        return result

    async def plan(self, actor: Actor, payload: dict, attempt: str, request_id: str) -> dict:
        started, response = time.perf_counter(), None
        token = self.credentials.issue(actor.principal, payload, attempt)
        try:
            response = await self.http.post(str(self.settings.planning_base_url).rstrip("/") + "/internal/time-fragment/plan",
                                            json=payload,
                                            headers={"Authorization": "Bearer " + token, "X-Request-ID": request_id},
                                            timeout=55)
            if response.status_code in (413, 422):
                detail = response.json()["detail"]
                raise failure(detail["code"], response.status_code, message=detail.get("message", "invalid planning input"))
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            log_failure("planning", "internal_time_fragment_plan", error, started, 1, response, request_id)
            raise failure("MODEL_GATEWAY_UNAVAILABLE", 503) from error
