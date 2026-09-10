#!/usr/bin/env python3
"""One bounded, synthetic request through the public AI planning API."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import re
import runpy
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

VALIDATOR = runpy.run_path(str(Path(__file__).with_name("validate-time-fragment-smoke.py")))
NOTIFIER = runpy.run_path(str(Path(__file__).with_name("notify-ai-planning.py")))
TEXT = "从 09:00 开始\n08:00 开始 Production Smoke，持续30分钟。"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_RESPONSE_CODES = frozenset({
    "ACCOUNT_SERVICE_UNAVAILABLE", "ACCOUNT_UNAVAILABLE", "AI_ACCOUNT_REQUIRED",
    "AI_DAILY_QUOTA_EXHAUSTED", "AI_QUOTA_EXHAUSTED", "AI_REQUEST_ALREADY_COMPLETED",
    "AI_REQUEST_ID_CONFLICT", "AI_REQUEST_IN_PROGRESS", "DEVELOPMENT_MEMBERSHIP_DISABLED",
    "EARLIEST_START_REQUIRED", "INPUT_TOO_LARGE", "INTERNAL_PLANNING_DISABLED",
    "INVALID_TIME_RANGE", "MODEL_GATEWAY_ERROR", "MODEL_GATEWAY_UNAVAILABLE",
    "PLANNING_DATE_NOT_ALLOWED", "SUPPORT_CODE_NOT_FOUND", "UNAUTHORIZED",
})


class ProbeFailure(Exception):
    def __init__(self, stage, code, status=None, request_id=None, response_request_id=None):
        self.stage, self.code, self.status = stage, code, status
        self.request_id = request_id
        self.response_request_id = response_request_id


def safe_request_id(value):
    return value if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) else None


def response_error_code(body):
    detail = body.get("detail", {}) if isinstance(body, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if code in SAFE_RESPONSE_CODES else "HTTP_ERROR"


def transport_error_code(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return "DNS_ERROR"
    if isinstance(reason, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "NETWORK_TIMEOUT"
    if isinstance(reason, (ConnectionError, ConnectionResetError, ConnectionRefusedError)):
        return "CONNECTION_ERROR"
    return "NETWORK_ERROR"


def stage_for_path(path):
    if path == "/health/ready":
        return "health"
    if path == "/api/auth/guest":
        return "auth"
    if path == "/api/plan/parse":
        return "plan"
    if path.startswith("/admin/time-fragment/quotas/"):
        return "probe_quota"
    return "configuration"


def safe_route(path):
    if path.startswith("/admin/time-fragment/quotas/"):
        return "/admin/time-fragment/quotas/{supportCode}/reset"
    return path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self, base):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ProbeFailure("configuration", "INVALID_BASE_URL")
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, payload=None, token=None, request_id=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if request_id:
            headers["X-Request-ID"] = request_id
        request = urllib.request.Request(self.base + path,
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode(), headers)
        try:
            response = self.opener.open(request, timeout=60 if path == "/api/plan/parse" else 10)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise ProbeFailure(path, transport_error_code(error), request_id=safe_request_id(request_id)) from None
        try:
            with response:
                status = response.status
                response_request_id = safe_request_id(response.headers.get("X-Request-ID"))
                raw = response.read(1_048_577)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise ProbeFailure(path, transport_error_code(error), request_id=safe_request_id(request_id)) from None
        if len(raw) > 1_048_576:
            raise ProbeFailure(path, "RESPONSE_TOO_LARGE", status, safe_request_id(request_id), response_request_id)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProbeFailure(path, "INVALID_JSON", status, safe_request_id(request_id), response_request_id) from None
        return status, body, response_request_id


class ObservedHTTP:
    def __init__(self, client):
        self.client = client
        self.checks = []

    def request(self, path, payload=None, token=None, request_id=None):
        started = time.monotonic()
        stage = stage_for_path(path)
        try:
            response = self.client.request(path, payload, token, request_id)
        except ProbeFailure as error:
            error.stage = stage
            error.request_id = error.request_id or safe_request_id(request_id)
            record = {"stage": stage, "route": safe_route(path),
                      "durationMs": round((time.monotonic() - started) * 1000),
                      "code": error.code}
            if error.status is not None:
                record["status"] = error.status
            if error.request_id:
                record["requestID"] = error.request_id
            if error.response_request_id:
                record["responseRequestID"] = error.response_request_id
            self.checks.append(record)
            raise
        status, body, response_request_id = response
        record = {"stage": stage, "route": safe_route(path), "status": status,
                  "durationMs": round((time.monotonic() - started) * 1000)}
        if safe_request_id(request_id):
            record["requestID"] = request_id
        if response_request_id:
            record["responseRequestID"] = response_request_id
        if status != 200:
            record["code"] = response_error_code(body)
        self.checks.append(record)
        return response


def require_success(stage, response, request_id=None):
    status, body, response_request_id = response
    if status != 200:
        raise ProbeFailure(stage, response_error_code(body), status, safe_request_id(request_id), response_request_id)
    return body


def make_payload(now):
    day = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
    plan = {"date": day, "items": []}
    projection = {"currentPlan": plan, "hiddenPendingDeletionOccurrenceSnapshots": []}
    digest = hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"text": TEXT, "requestID": str(uuid.uuid4()), "baseFingerprint": "sha256:" + digest,
            "currentPlan": plan, "now": now.isoformat(), "earliestStartSlot": 36}


def check(client, device_id, now, support_code="", admin_key_file=None, run_id=None):
    run_id = run_id or str(uuid.uuid4())
    if not re.fullmatch(r"ai-planning-hourly-probe-[a-f0-9-]{36}", device_id):
        raise ProbeFailure("configuration", "DEDICATED_PROBE_ID_REQUIRED")
    health_id = run_id + "-health"
    health_response = client.request("/health/ready", request_id=health_id)
    health = require_success("health", health_response, health_id)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise ProbeFailure("health", "NOT_READY", request_id=health_id,
                           response_request_id=health_response[2])
    auth_id = run_id + "-auth"
    auth_response = client.request("/api/auth/guest", {"device_id": device_id}, request_id=auth_id)
    auth = require_success("auth", auth_response, auth_id)
    token = auth.get("access_token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise ProbeFailure("auth", "MISSING_TOKEN", request_id=auth_id,
                           response_request_id=auth_response[2])
    payload, http_id = make_payload(now), run_id + "-plan"
    response = client.request("/api/plan/parse", payload, token, http_id)
    detail = response[1].get("detail", {}) if isinstance(response[1], dict) else {}
    quota_reset = False
    if response[0] == 429 and isinstance(detail, dict) and detail.get("code") == "AI_QUOTA_EXHAUSTED":
        # Only replenish this deployment's pre-bound probe identity, never a user or a global quota.
        if not re.fullmatch(r"TF-[A-Z2-7]{4}-[A-Z2-7]{4}", support_code) or detail.get("supportCode") != support_code or admin_key_file is None:
            raise ProbeFailure("probe_quota", "PROBE_QUOTA_CONFIGURATION_REQUIRED", 429,
                               http_id, response[2])
        key = Path(admin_key_file).read_text().strip()
        if not key:
            raise ProbeFailure("probe_quota", "MISSING_ADMIN_CREDENTIAL",
                               request_id=http_id, response_request_id=response[2])
        reset_id = run_id + "-quota-reset"
        require_success("probe_quota", client.request("/admin/time-fragment/quotas/" + support_code + "/reset",
                                                      {}, key, reset_id), reset_id)
        quota_reset = True
        # A quota rejection has made no model call. Reuse the logical request ID for the one retry.
        response = client.request("/api/plan/parse", payload, token, http_id)
    body = require_success("plan", response, http_id)
    if response[2] != http_id:
        raise ProbeFailure("plan", "REQUEST_ID_MISMATCH", request_id=http_id,
                           response_request_id=response[2])
    try:
        attempts, count = VALIDATOR["validate_response"](body, payload["requestID"], payload["baseFingerprint"], payload["currentPlan"]["date"])
        item = body["proposal"]["candidatePlan"]["items"][0]
        if count != 1 or item["segments"] != [{"startSlot": 32, "endSlot": 34}]:
            raise ValueError("unexpected placement")
    except (ValueError, TypeError, KeyError, IndexError):
        raise ProbeFailure("plan", "INVALID_PLAN", request_id=http_id,
                           response_request_id=response[2]) from None
    return {"status": "healthy", "probeRunID": run_id, "requestID": http_id, "modelAttempts": attempts,
            "date": payload["currentPlan"]["date"], "start": "08:00", "end": "08:30", "probeQuotaReset": quota_reset}


def transition(previous, current):
    if current == "unhealthy":
        return "still_failing" if previous == "unhealthy" else "failure"
    return "recovered" if previous == "unhealthy" else "healthy"


def save_result(directory, result):
    serialized = json.dumps(result, ensure_ascii=False)
    temporary = directory / "latest.tmp"
    temporary.write_text(serialized + "\n")
    temporary.replace(directory / "latest.json")
    return serialized


def main():
    os.umask(0o077)
    directory = Path(os.environ.get("PROBE_STATE_DIR", "/var/lib/model-server-hourly-probe"))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "already_running"}))
            return 0
        previous = json.loads((directory / "latest.json").read_text()) if (directory / "latest.json").exists() else {}
        started, now = time.monotonic(), datetime.now(ZoneInfo("Asia/Shanghai"))
        admin_key_file = os.environ.get("PROBE_ADMIN_KEY_FILE")
        if not admin_key_file and os.environ.get("CREDENTIALS_DIRECTORY"):
            admin_key_file = str(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "admin-key")
        run_id = str(uuid.uuid4())
        http = None
        try:
            http = ObservedHTTP(HTTPClient(os.environ.get("PROBE_BASE_URL", "https://api.keeline.xyz")))
            result = check(http,
                           os.environ.get("PROBE_DEVICE_ID", ""), now,
                           os.environ.get("PROBE_SUPPORT_CODE", ""), admin_key_file, run_id)
        except ProbeFailure as error:
            result = {"status": "unhealthy", "probeRunID": run_id, "stage": error.stage,
                      "code": error.code, "httpStatus": error.status}
            if error.request_id:
                result["requestID"] = error.request_id
            if error.response_request_id:
                result["responseRequestID"] = error.response_request_id
        except OSError:
            result = {"status": "unhealthy", "probeRunID": run_id,
                      "stage": "configuration", "code": "LOCAL_IO_ERROR"}
        result["checks"] = http.checks if http else []
        result.update({"checkedAt": now.isoformat(), "durationSeconds": round(time.monotonic() - started, 3),
                       "event": transition(previous.get("status"), result["status"])})
        save_result(directory, result)
        result["notification"] = NOTIFIER["notify"](directory, result)
        serialized = save_result(directory, result)
        print(serialized)
        return 0 if result["status"] == "healthy" and result["notification"]["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
