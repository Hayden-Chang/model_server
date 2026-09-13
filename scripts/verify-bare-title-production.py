#!/usr/bin/env python3
"""Exercise the production planning cases protected by the bare-title fix."""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def post_json(url: str, payload: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise AssertionError(f"{url} returned HTTP {error.code}: {detail}") from error


def fingerprint(plan_date: str, items: list[dict[str, Any]]) -> str:
    projection = {
        "currentPlan": {"date": plan_date, "items": items},
        "hiddenPendingDeletionOccurrenceSnapshots": [],
    }
    canonical = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def planning_request(text: str, plan_date: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    return {
        "text": text,
        "requestID": str(uuid.uuid4()),
        "baseFingerprint": fingerprint(plan_date, items),
        "currentPlan": {"date": plan_date, "items": items},
        "now": f"{plan_date}T00:00:00+08:00",
        "earliestStartSlot": 36,
    }


def operations(response: dict[str, Any], *, require_valid: bool) -> list[dict[str, Any]]:
    validation = response.get("validation")
    assert isinstance(validation, dict), "response.validation is missing"
    if require_valid:
        assert validation.get("valid") is True, f"planning validation failed: {validation}"
    proposal = response.get("proposal")
    if proposal is None:
        return []
    assert isinstance(proposal, dict), "response.proposal must be an object or null"
    result = proposal.get("operations")
    assert isinstance(result, list), "response.proposal.operations must be an array"
    return result


def assert_exact_bare_title(response: dict[str, Any], title: str) -> None:
    result = operations(response, require_valid=True)
    assert len(result) == 1, f"{title!r} must produce exactly one operation: {result}"
    operation = result[0]
    assert operation.get("type") == "add", f"{title!r} must produce an add operation"
    assert operation.get("title") == title, f"{title!r} was rewritten: {operation}"
    assert operation.get("durationSlots") == 2, f"{title!r} must use the 30-minute default"
    assert operation.get("priority") is None, f"{title!r} must not invent priority"
    candidate = response["proposal"]["candidatePlan"]["items"]
    assert len(candidate) == 1 and candidate[0].get("title") == title
    assert candidate[0].get("segments") == [{"startSlot": 36, "endSlot": 38}]


def run(base_url: str) -> None:
    auth = post_json(
        f"{base_url}/api/auth/guest",
        {"device_id": f"bare-title-production-smoke-{uuid.uuid4()}"},
    )
    token = auth.get("access_token")
    assert isinstance(token, str) and token, "guest authentication returned no token"
    plan_date = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()

    for title in ("爸爸", "一点想法"):
        response = post_json(
            f"{base_url}/api/plan/parse",
            planning_request(title, plan_date),
            token=token,
        )
        assert_exact_bare_title(response, title)
        print(f"PASS exact bare title: {title}")

    clock_response = post_json(
        f"{base_url}/api/plan/parse",
        planning_request("五点吃饭", plan_date),
        token=token,
    )
    clock_operations = operations(clock_response, require_valid=True)
    assert len(clock_operations) == 1 and clock_operations[0].get("type") == "add"
    assert clock_operations[0].get("title") == "吃饭", f"clock request was misclassified: {clock_operations}"
    clock_items = clock_response["proposal"]["candidatePlan"]["items"]
    assert len(clock_items) == 1
    assert clock_items[0].get("segments") == [{"startSlot": 68, "endSlot": 70}]
    print("PASS Chinese clock: 五点吃饭")

    for command in ("删除性能", "把性能移到下午"):
        command_response = post_json(
            f"{base_url}/api/plan/parse",
            planning_request(command, plan_date),
            token=token,
        )
        command_operations = operations(command_response, require_valid=False)
        assert not any(
            operation.get("type") == "add" for operation in command_operations
        ), f"command was incorrectly converted to an add: {command_operations}"
        print(f"PASS command boundary: {command}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", help="production base URL, for example https://api.example.com")
    arguments = parser.parse_args()
    try:
        run(arguments.base_url.rstrip("/"))
    except (AssertionError, OSError, ValueError) as error:
        print(f"bare-title production smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
