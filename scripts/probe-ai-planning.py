#!/usr/bin/env python3
"""One bounded, synthetic request through the public AI planning API."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import runpy
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
TEXT = "从 09:00 开始\n08:00 开始 Production Smoke，持续30分钟。"
SAFE_CODES = {"MODEL_GATEWAY_ERROR", "MODEL_GATEWAY_UNAVAILABLE", "AI_QUOTA_EXHAUSTED",
              "AI_DAILY_QUOTA_EXHAUSTED", "AI_REQUEST_IN_PROGRESS", "AI_REQUEST_ALREADY_COMPLETED"}


class ProbeFailure(Exception):
    def __init__(self, stage, code, status=None):
        self.stage, self.code, self.status = stage, code, status


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
        except (OSError, urllib.error.URLError):
            raise ProbeFailure(path, "NETWORK_OR_TIMEOUT") from None
        with response:
            status = response.status
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ProbeFailure(path, "RESPONSE_TOO_LARGE", status)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ProbeFailure(path, "INVALID_JSON", status) from None
            return status, body, response.headers.get("X-Request-ID")


def require_success(stage, response):
    status, body, _ = response
    if status != 200:
        detail = body.get("detail", {}) if isinstance(body, dict) else {}
        code = detail.get("code") if isinstance(detail, dict) else None
        raise ProbeFailure(stage, code if isinstance(code, str) and code in SAFE_CODES else "HTTP_ERROR", status)
    return body


def make_payload(now):
    day = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
    plan = {"date": day, "items": []}
    projection = {"currentPlan": plan, "hiddenPendingDeletionOccurrenceSnapshots": []}
    digest = hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"text": TEXT, "requestID": str(uuid.uuid4()), "baseFingerprint": "sha256:" + digest,
            "currentPlan": plan, "now": now.isoformat(), "earliestStartSlot": 36}


def check(client, device_id, now, support_code="", admin_key_file=None):
    if not re.fullmatch(r"ai-planning-hourly-probe-[a-f0-9-]{36}", device_id):
        raise ProbeFailure("configuration", "DEDICATED_PROBE_ID_REQUIRED")
    health = require_success("health", client.request("/health/ready"))
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise ProbeFailure("health", "NOT_READY")
    auth = require_success("auth", client.request("/api/auth/guest", {"device_id": device_id}))
    token = auth.get("access_token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise ProbeFailure("auth", "MISSING_TOKEN")
    payload, http_id = make_payload(now), str(uuid.uuid4())
    response = client.request("/api/plan/parse", payload, token, http_id)
    detail = response[1].get("detail", {}) if isinstance(response[1], dict) else {}
    quota_reset = False
    if response[0] == 429 and isinstance(detail, dict) and detail.get("code") == "AI_QUOTA_EXHAUSTED":
        # Only replenish this deployment's pre-bound probe identity, never a user or a global quota.
        if not re.fullmatch(r"TF-[A-Z2-7]{4}-[A-Z2-7]{4}", support_code) or detail.get("supportCode") != support_code or admin_key_file is None:
            raise ProbeFailure("probe_quota", "PROBE_QUOTA_CONFIGURATION_REQUIRED", 429)
        key = Path(admin_key_file).read_text().strip()
        if not key:
            raise ProbeFailure("probe_quota", "MISSING_ADMIN_CREDENTIAL")
        require_success("probe_quota", client.request("/admin/time-fragment/quotas/" + support_code + "/reset", {}, key))
        quota_reset = True
        # A quota rejection has made no model call. Reuse the logical request ID for the one retry.
        response = client.request("/api/plan/parse", payload, token, http_id)
    body = require_success("plan", response)
    if response[2] != http_id:
        raise ProbeFailure("plan", "REQUEST_ID_MISMATCH")
    try:
        attempts, count = VALIDATOR["validate_response"](body, payload["requestID"], payload["baseFingerprint"], payload["currentPlan"]["date"])
        item = body["proposal"]["candidatePlan"]["items"][0]
        if count != 1 or item["segments"] != [{"startSlot": 32, "endSlot": 34}]:
            raise ValueError("unexpected placement")
    except (ValueError, TypeError, KeyError, IndexError):
        raise ProbeFailure("plan", "INVALID_PLAN") from None
    return {"status": "healthy", "requestID": http_id, "modelAttempts": attempts,
            "date": payload["currentPlan"]["date"], "start": "08:00", "end": "08:30", "probeQuotaReset": quota_reset}


def transition(previous, current):
    if current == "unhealthy":
        return "still_failing" if previous == "unhealthy" else "failure"
    return "recovered" if previous == "unhealthy" else "healthy"


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
        try:
            result = check(HTTPClient(os.environ.get("PROBE_BASE_URL", "https://api.keeline.xyz")),
                           os.environ.get("PROBE_DEVICE_ID", ""), now,
                           os.environ.get("PROBE_SUPPORT_CODE", ""), os.environ.get("PROBE_ADMIN_KEY_FILE"))
        except ProbeFailure as error:
            result = {"status": "unhealthy", "stage": error.stage, "code": error.code, "httpStatus": error.status}
        except OSError:
            result = {"status": "unhealthy", "stage": "configuration", "code": "LOCAL_IO_ERROR"}
        result.update({"checkedAt": now.isoformat(), "durationSeconds": round(time.monotonic() - started, 3),
                       "event": transition(previous.get("status"), result["status"])})
        serialized = json.dumps(result, ensure_ascii=False)
        temporary = directory / "latest.tmp"
        temporary.write_text(serialized + "\n")
        temporary.replace(directory / "latest.json")
        print(serialized)
        return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
