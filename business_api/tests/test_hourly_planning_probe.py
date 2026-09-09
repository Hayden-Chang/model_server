import copy
import fcntl
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.guest_auth import GuestTokenCodec
from app.quota_store import QuotaStore
from test_time_fragment_api import FakeModelClient, model_add, operations_output, settings
from test_time_fragment_smoke import valid_smoke_response

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("hourly_probe", ROOT / "scripts/probe-ai-planning.py")
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
DEVICE = "ai-planning-hourly-probe-10000000-0000-4000-8000-000000000001"
NOW = datetime.fromisoformat("2026-09-09T10:00:00+08:00")
CODE = "TF-AAAA-BBBB"


class FakeHTTP:
    def __init__(self, plan_status=200, detail=None, mutate=None):
        self.calls = []
        self.plan_status, self.detail, self.mutate = plan_status, detail, mutate

    def request(self, path, payload=None, token=None, request_id=None):
        self.calls.append((path, copy.deepcopy(payload), token, request_id))
        if path == "/health/ready":
            return 200, {"status": "ready"}, None
        if path == "/api/auth/guest":
            return 200, {"access_token": "private-guest-token"}, None
        if path.startswith("/admin/"):
            self.plan_status = 200
            return 200, {"remaining": 50}, None
        if self.plan_status != 200:
            return self.plan_status, {"detail": self.detail}, request_id
        result = valid_smoke_response()
        result["requestID"] = payload["requestID"]
        result["proposal"]["baseFingerprint"] = payload["baseFingerprint"]
        result["proposal"]["candidatePlan"]["date"] = payload["currentPlan"]["date"]
        if self.mutate:
            self.mutate(result)
        return 200, result, request_id


def test_valid_request_checks_public_route_and_exact_earlier_placement():
    http = FakeHTTP()
    result = PROBE.check(http, DEVICE, NOW)
    assert result["status"] == "healthy"
    assert (result["start"], result["end"]) == ("08:00", "08:30")
    assert [c[0] for c in http.calls] == ["/health/ready", "/api/auth/guest", "/api/plan/parse"]
    payload = http.calls[-1][1]
    assert payload["earliestStartSlot"] == 36
    assert payload["currentPlan"] == {"date": "2026-09-10", "items": []}
    assert payload["requestID"] != http.calls[-1][3]
    assert not any("apply" in c[0] for c in http.calls)


def test_tomorrow_uses_shanghai_day_at_utc_boundary():
    payload = PROBE.make_payload(datetime.fromisoformat("2026-09-09T17:01:00+00:00"))
    assert payload["currentPlan"]["date"] == "2026-09-11"


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(proposal=None),
    lambda r: r["validation"].update(valid=False),
    lambda r: r["proposal"]["candidatePlan"]["items"][0].update(segments=[{"startSlot": 36, "endSlot": 38}]),
    lambda r: r["proposal"]["candidatePlan"]["items"][0].update(segments=[]),
    lambda r: r.update(requestID="stale-request"),
    lambda r: r["proposal"].update(baseFingerprint="stale-fingerprint"),
])
def test_http_200_does_not_hide_invalid_or_wrong_plan(mutate):
    with pytest.raises(PROBE.ProbeFailure) as failure:
        PROBE.check(FakeHTTP(mutate=mutate), DEVICE, NOW)
    assert failure.value.code == "INVALID_PLAN"


@pytest.mark.parametrize("status,code", [(502, "MODEL_GATEWAY_ERROR"), (503, "MODEL_GATEWAY_UNAVAILABLE"), (401, "not_a_safe_code")])
def test_model_or_auth_failures_do_not_retry_or_print_upstream_secrets(status, code):
    http = FakeHTTP(status, {"code": code, "message": "private-key-and-prompt"})
    with pytest.raises(PROBE.ProbeFailure) as failure:
        PROBE.check(http, DEVICE, NOW)
    assert len([c for c in http.calls if c[0] == "/api/plan/parse"]) == 1
    assert failure.value.status == status
    assert "private" not in repr(vars(failure.value))


@pytest.mark.parametrize("response_code,bound_code,error_code", [
    (CODE, "", "AI_QUOTA_EXHAUSTED"),
    ("TF-CCCC-DDDD", CODE, "AI_QUOTA_EXHAUSTED"),
    (CODE, CODE, "AI_DAILY_QUOTA_EXHAUSTED"),
])
def test_quota_recovery_never_resets_an_unbound_or_membership_identity(response_code, bound_code, error_code, tmp_path):
    key = tmp_path / "key"
    key.write_text("private-admin-key")
    http = FakeHTTP(429, {"code": error_code, "supportCode": response_code})
    with pytest.raises(PROBE.ProbeFailure):
        PROBE.check(http, DEVICE, NOW, bound_code, key)
    assert not any(c[0].startswith("/admin/") for c in http.calls)


def test_probe_identity_cannot_be_an_ordinary_app_identity():
    http = FakeHTTP()
    with pytest.raises(PROBE.ProbeFailure):
        PROBE.check(http, "daymosaic-a907", NOW)
    assert http.calls == []


def test_exhausted_probe_quota_is_restored_without_touching_other_user(settings, tmp_path):
    quotas = QuotaStore(":memory:", 1)
    probe = quotas.reserve(GuestTokenCodec.device_key(DEVICE), "previous-probe")
    other = quotas.reserve(GuestTokenCodec.device_key("ordinary-user"), "other-request")
    quotas.consume(probe)
    quotas.consume(other)
    fake = FakeModelClient([operations_output([model_add("Production Smoke", "08:00 开始 Production Smoke，持续30分钟",
                                                       start_time="08:00", start_evidence="08:00 开始 Production Smoke")])])
    key = tmp_path / "key"
    key.write_text(settings.admin_api_key.get_secret_value())
    paths = []
    with TestClient(create_app(settings, fake, quota_store=quotas)) as client:
        class Adapter:
            def request(self, path, payload=None, token=None, request_id=None):
                paths.append(path)
                headers = {"Authorization": "Bearer " + token} if token else {}
                if request_id:
                    headers["X-Request-ID"] = request_id
                response = client.request("GET" if payload is None else "POST", path, json=payload, headers=headers)
                return response.status_code, response.json(), response.headers.get("X-Request-ID")
        result = PROBE.check(Adapter(), DEVICE, NOW, probe.support_code, key)
        assert result["status"] == "healthy" and result["probeQuotaReset"] is True
        assert len(fake.calls) == 1
        assert paths.count("/api/plan/parse") == 2
        assert quotas.status(other.support_code).used == 1
        assert quotas.status(other.support_code).remaining == 0
        assert quotas.status(probe.support_code).used == 1


@pytest.mark.parametrize("previous,current,event", [(None, "unhealthy", "failure"), ("unhealthy", "unhealthy", "still_failing"),
    ("unhealthy", "healthy", "recovered"), ("healthy", "healthy", "healthy")])
def test_notification_events_distinguish_failures_and_recovery(previous, current, event):
    assert PROBE.transition(previous, current) == event


def test_process_lock_skips_overlapping_probe_without_request(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROBE_STATE_DIR", str(tmp_path))
    with (tmp_path / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert PROBE.main() == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "already_running"


@pytest.mark.parametrize("fails,exit_code,event", [(True, 1, "still_failing"), (False, 0, "recovered")])
def test_run_persists_sanitized_result_and_status_transition(tmp_path, monkeypatch, capsys, fails, exit_code, event):
    monkeypatch.setenv("PROBE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROBE_DEVICE_ID", DEVICE)
    (tmp_path / "latest.json").write_text(json.dumps({"status": "unhealthy"}))
    http = FakeHTTP(502, {"code": "MODEL_GATEWAY_ERROR", "message": "private-upstream-secret"}) if fails else FakeHTTP()
    monkeypatch.setattr(PROBE, "HTTPClient", lambda base: http)
    assert PROBE.main() == exit_code
    output = capsys.readouterr().out
    record = json.loads((tmp_path / "latest.json").read_text())
    assert record == json.loads(output)
    assert record["event"] == event
    assert "checkedAt" in record and record["durationSeconds"] >= 0
    assert "private" not in output
    assert not (tmp_path / "latest.tmp").exists()


@pytest.mark.parametrize("url", ["http://api.keeline.xyz", "https://secret@api.keeline.xyz", "https://api.keeline.xyz/path", "https://api.keeline.xyz?key=private"])
def test_credentials_only_target_a_pathless_https_origin(url):
    with pytest.raises(PROBE.ProbeFailure):
        PROBE.HTTPClient(url)


def test_redirects_never_forward_credentials_to_another_origin():
    assert PROBE.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid") is None


def test_systemd_credentials_directory_supports_quota_recovery(tmp_path, monkeypatch, capsys):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "admin-key").write_text("private-admin-key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.delenv("PROBE_ADMIN_KEY_FILE", raising=False)
    monkeypatch.setenv("PROBE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PROBE_DEVICE_ID", DEVICE)
    monkeypatch.setenv("PROBE_SUPPORT_CODE", CODE)
    http = FakeHTTP(429, {"code": "AI_QUOTA_EXHAUSTED", "supportCode": CODE})
    monkeypatch.setattr(PROBE, "HTTPClient", lambda base: http)
    assert PROBE.main() == 0
    assert json.loads(capsys.readouterr().out)["probeQuotaReset"] is True
    assert http.calls[3][0] == "/admin/time-fragment/quotas/" + CODE + "/reset"
    assert http.calls[3][2] == "private-admin-key"
