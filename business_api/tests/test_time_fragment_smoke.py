import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts" / "validate-time-fragment-smoke.py"


def load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("time_fragment_smoke_validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


def valid_smoke_response(*, title: str = "Production Smoke") -> dict[str, Any]:
    temporary_id = "11111111-1111-4111-8111-111111111111"
    return {
        "requestID": "app-request",
        "proposal": {
            "baseFingerprint": "sha256:fingerprint",
            "algorithmVersion": "time-fragment-planner-v1",
            "deletedOccurrenceIDs": [],
            "deletedExternalEventIDs": [],
            "operations": [
                {
                    "type": "add",
                    "temporaryId": temporary_id,
                    "title": title,
                    "durationSlots": 2,
                    "placement": None,
                    "priority": None,
                    "inputOrder": 0,
                }
            ],
            "candidatePlan": {
                "date": "2026-08-24",
                "items": [
                    {
                        "itemId": temporary_id,
                        "objectType": "internalTask",
                        "domainRef": None,
                        "title": title,
                        "durationSlots": 2,
                        "segments": [{"startSlot": 32, "endSlot": 34}],
                        "isPinned": False,
                        "isCompleted": False,
                    }
                ],
            },
        },
        "validation": {"valid": True, "attempts": 1, "issues": []},
    }


def test_smoke_validator_accepts_complete_expected_response() -> None:
    assert VALIDATOR.validate_response(
        valid_smoke_response(),
        "app-request",
        "sha256:fingerprint",
        "2026-08-24",
    ) == (1, 1)


def test_smoke_validator_rejects_a_self_consistent_but_wrong_title() -> None:
    with pytest.raises(VALIDATOR.SmokeValidationError, match="Production Smoke"):
        VALIDATOR.validate_response(
            valid_smoke_response(title="Wrong Smoke Title"),
            "app-request",
            "sha256:fingerprint",
            "2026-08-24",
        )


def test_smoke_validator_rejects_forbidden_public_fields() -> None:
    response = valid_smoke_response()
    response["proposal"]["operations"][0]["authorizationText"] = "private evidence"

    with pytest.raises(VALIDATOR.SmokeValidationError, match="forbidden keys"):
        VALIDATOR.validate_response(
            response,
            "app-request",
            "sha256:fingerprint",
            "2026-08-24",
        )


def test_verify_production_sends_ios_canonical_fingerprint_and_curl_timeouts(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_request = tmp_path / "request.json"
    captured_http_request_id = tmp_path / "http-request-id.txt"
    curl_log = tmp_path / "curl.jsonl"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["CURL_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

url = args[-1]

def option(name):
    return args[args.index(name) + 1] if name in args else None

if url.endswith("/api/auth/guest"):
    print(json.dumps({"access_token": "guest-token"}))
elif url.endswith("/api/plan/parse"):
    request_path = option("--data-binary")[1:]
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    Path(os.environ["CAPTURED_REQUEST"]).write_text(
        json.dumps(request, separators=(",", ":")),
        encoding="utf-8",
    )
    request_id_header = next(
        header.split(": ", 1)[1]
        for index, header in enumerate(args)
        if index > 0 and args[index - 1] == "-H" and header.startswith("X-Request-ID: ")
    )
    Path(os.environ["CAPTURED_HTTP_REQUEST_ID"]).write_text(
        request_id_header,
        encoding="utf-8",
    )
    Path(option("--dump-header")).write_text(
        f"HTTP/1.1 200 OK\\r\\nX-Request-ID: {request_id_header}\\r\\n\\r\\n",
        encoding="utf-8",
    )
    temporary_id = "11111111-1111-4111-8111-111111111111"
    response = {
        "requestID": request["requestID"],
        "proposal": {
            "baseFingerprint": request["baseFingerprint"],
            "algorithmVersion": "time-fragment-planner-v1",
            "deletedOccurrenceIDs": [],
            "deletedExternalEventIDs": [],
            "operations": [{
                "type": "add",
                "temporaryId": temporary_id,
                "title": "Production Smoke",
                "durationSlots": 2,
                "placement": None,
                "priority": None,
                "inputOrder": 0,
            }],
            "candidatePlan": {
                "date": request["currentPlan"]["date"],
                "items": [{
                    "itemId": temporary_id,
                    "objectType": "internalTask",
                    "domainRef": None,
                    "title": "Production Smoke",
                    "durationSlots": 2,
                    "segments": [{"startSlot": 0, "endSlot": 2}],
                    "isPinned": False,
                    "isCompleted": False,
                }],
            },
        },
        "validation": {"valid": True, "attempts": 1, "issues": []},
    }
    Path(option("--output")).write_text(json.dumps(response), encoding="utf-8")
elif "/admin/observability/summary?" in url:
    summary = {
        "totals": {
            "request_count": 1,
            "model_call_count": 1,
            "token_reported_requests": 1,
            "total_tokens": 9,
        }
    }
    Path(option("--output")).write_text(json.dumps(summary), encoding="utf-8")
elif "/admin/observability/requests?" in url:
    request_id = Path(os.environ["CAPTURED_HTTP_REQUEST_ID"]).read_text(encoding="utf-8")
    detail = {
        "records": [{
            "request_id": request_id,
            "usage": {"total_tokens": 9},
        }]
    }
    Path(option("--output")).write_text(json.dumps(detail), encoding="utf-8")
else:
    print("{}")
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PUBLIC_DOMAIN": "smoke.example.test",
        "BUSINESS_API_KEY": "business-key",
        "ADMIN_API_KEY": "admin-key",
        "CAPTURED_REQUEST": str(captured_request),
        "CAPTURED_HTTP_REQUEST_ID": str(captured_http_request_id),
        "CURL_LOG": str(curl_log),
    }

    completed = subprocess.run(
        ["sh", str(ROOT / "scripts" / "verify-production.sh")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    request = json.loads(captured_request.read_bytes())
    canonical_projection = json.dumps(
        {
            "currentPlan": request["currentPlan"],
            "hiddenPendingDeletionOccurrenceSnapshots": [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert request["baseFingerprint"] == "sha256:" + hashlib.sha256(
        canonical_projection
    ).hexdigest()

    curl_calls = [json.loads(line) for line in curl_log.read_text(encoding="utf-8").splitlines()]
    assert len(curl_calls) == 7
    for call in curl_calls:
        assert "--connect-timeout" in call
        assert "--max-time" in call
        assert int(call[call.index("--connect-timeout") + 1]) > 0
        assert int(call[call.index("--max-time") + 1]) > 0
