#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


PIPELINE_ID = "time-fragment-plan-v2"


class RuntimeSwitchError(Exception):
    pass


RequestFunction = Callable[..., dict[str, Any]]
ProbeFunction = Callable[[str], None]


def switch_pipeline(
    *,
    base_url: str,
    admin_key: str,
    pipeline_id: str,
    model_alias: str | None,
    thinking_mode: str,
    reasoning_effort: str | None,
    run_probe: bool,
    request: RequestFunction,
    probe: ProbeFunction,
) -> dict[str, Any]:
    path = f"/admin/runtime/pipelines/{pipeline_id}"
    current = request(base_url, path, token=admin_key)
    desired = {
        "modelAlias": model_alias or current["modelAlias"],
        "thinkingMode": thinking_mode,
        "reasoningEffort": reasoning_effort,
    }
    if all(current[key] == value for key, value in desired.items()):
        return {"changed": False, "probed": False, "config": current}

    updated = request(
        base_url,
        path,
        method="PUT",
        token=admin_key,
        payload={**desired, "expectedVersion": current["version"]},
    )
    try:
        if run_probe:
            probe(base_url)
    except Exception as error:
        rollback_payload = {
            "modelAlias": current["modelAlias"],
            "thinkingMode": current["thinkingMode"],
            "reasoningEffort": current["reasoningEffort"],
            "expectedVersion": updated["version"],
        }
        rolled_back = request(
            base_url,
            path,
            method="PUT",
            token=admin_key,
            payload=rollback_payload,
        )
        raise RuntimeSwitchError(
            f"probe failed; restored pipeline runtime configuration at version {rolled_back['version']}: {error}"
        ) from error
    return {"changed": True, "probed": run_probe, "config": updated}


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeSwitchError(f"{method} {path} returned HTTP {error.code}: {body[:500]}") from error
    except OSError as error:
        raise RuntimeSwitchError(f"{method} {path} failed: {error}") from error
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeSwitchError(f"{method} {path} returned invalid JSON") from error
    if not isinstance(parsed, dict):
        raise RuntimeSwitchError(f"{method} {path} returned a non-object JSON response")
    return parsed


def probe_time_fragment(base_url: str) -> None:
    health = request_json(base_url, "/health/ready")
    if health.get("status") != "ready":
        raise RuntimeSwitchError("service is not ready")

    device_id = f"runtime-config-probe-{uuid.uuid4().hex}"
    guest = request_json(
        base_url,
        "/api/auth/guest",
        method="POST",
        payload={"device_id": device_id},
    )
    token = guest.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeSwitchError("guest authentication did not return an access token")

    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    plan_date = today.isoformat()
    now = datetime.combine(today, time.min, tzinfo=zone)
    projection = {
        "currentPlan": {"date": plan_date, "items": []},
        "hiddenPendingDeletionOccurrenceSnapshots": [],
    }
    canonical = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "text": "Add one task named Runtime Config Probe using the default duration.",
        "requestID": str(uuid.uuid4()),
        "baseFingerprint": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "currentPlan": {"date": plan_date, "items": []},
        "now": now.isoformat(timespec="seconds"),
    }
    response = request_json(
        base_url,
        "/api/plan/parse",
        method="POST",
        token=token,
        payload=payload,
        timeout=120.0,
    )
    validation = response.get("validation")
    proposal = response.get("proposal")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        raise RuntimeSwitchError("V2 probe returned an invalid plan")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("candidatePlan"), dict):
        raise RuntimeSwitchError("V2 probe did not return a candidate plan")


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atomically switch a pipeline's persisted runtime model settings.")
    parser.add_argument("pipeline_id", choices=[PIPELINE_ID])
    parser.add_argument("--base-url", default=os.environ.get("PIPELINE_RUNTIME_BASE_URL"))
    parser.add_argument("--admin-key-file", default=os.environ.get("PIPELINE_RUNTIME_ADMIN_KEY_FILE"))
    parser.add_argument("--model-alias")
    parser.add_argument("--thinking", required=True, choices=["enabled", "disabled"])
    parser.add_argument("--effort", choices=["low", "high", "max"])
    parser.add_argument("--no-probe", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.base_url:
        parser.error("--base-url or PIPELINE_RUNTIME_BASE_URL is required")
    if arguments.thinking == "enabled" and arguments.effort is None:
        parser.error("--effort is required when thinking is enabled")
    if arguments.thinking == "disabled" and arguments.effort is not None:
        parser.error("--effort must be omitted when thinking is disabled")
    return arguments


def load_admin_key(path: str | None) -> str:
    if path:
        key = Path(path).read_text(encoding="utf-8").strip()
    else:
        key = os.environ.get("ADMIN_API_KEY", "").strip()
    if not key:
        raise RuntimeSwitchError("admin key is required via --admin-key-file or ADMIN_API_KEY")
    return key


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        result = switch_pipeline(
            base_url=arguments.base_url,
            admin_key=load_admin_key(arguments.admin_key_file),
            pipeline_id=arguments.pipeline_id,
            model_alias=arguments.model_alias,
            thinking_mode=arguments.thinking,
            reasoning_effort=arguments.effort,
            run_probe=not arguments.no_probe,
            request=request_json,
            probe=probe_time_fragment,
        )
    except (OSError, RuntimeSwitchError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
