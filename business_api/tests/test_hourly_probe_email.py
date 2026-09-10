import importlib.util
import json
import smtplib
import ssl
from pathlib import Path

import pytest

from test_hourly_planning_probe import DEVICE, FakeHTTP, PROBE

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("probe_email", ROOT / "scripts/notify-ai-planning.py")
MAIL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAIL)
FAILURE = {"status": "unhealthy", "checkedAt": "2026-09-09T12:00:00+08:00", "stage": "plan",
           "code": "MODEL_GATEWAY_ERROR", "httpStatus": 502, "probeRunID": "probe-run-123",
           "requestID": "probe-run-123-plan", "responseRequestID": "probe-run-123-plan"}
HEALTHY = {"status": "healthy", "checkedAt": "2026-09-09T13:00:00+08:00", "durationSeconds": 1.2}


@pytest.fixture
def configured_mail(tmp_path, monkeypatch):
    config = {"host": "smtp.example.com", "from": "sender@example.com", "to": "recipient@example.com",
              "username": "smtp-user", "password": "private-password", "security": "ssl"}
    path = tmp_path / "mail-config"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("PROBE_MAIL_CONFIG", str(path))
    return config


def test_only_first_failure_and_recovery_send_mail_across_runs(tmp_path, monkeypatch, configured_mail):
    messages = []
    monkeypatch.setattr(MAIL, "send_mail", lambda config, kind, result, incident: messages.append(kind))
    for result in (HEALTHY, FAILURE, FAILURE, FAILURE, HEALTHY, HEALTHY):
        MAIL.notify(tmp_path, result)
    assert messages == ["failure", "recovery"]
    assert json.loads((tmp_path / "mail-state.json").read_text()) == {}


def test_failed_delivery_retries_without_losing_incident(tmp_path, monkeypatch, configured_mail):
    def fail(*args):
        raise smtplib.SMTPAuthenticationError(535, b"private-password-and-server-detail")
    monkeypatch.setattr(MAIL, "send_mail", fail)
    assert MAIL.notify(tmp_path, FAILURE) == {"status": "failed", "code": "SMTP_AUTH_FAILED"}
    assert json.loads((tmp_path / "mail-state.json").read_text())["failureSent"] is False
    messages = []
    monkeypatch.setattr(MAIL, "send_mail", lambda config, kind, result, incident: messages.append(kind))
    assert MAIL.notify(tmp_path, FAILURE)["status"] == "accepted"
    assert MAIL.notify(tmp_path, FAILURE)["status"] == "quiet"
    assert messages == ["failure"]


def test_recovery_is_not_lost_when_failure_email_could_not_be_sent(tmp_path, monkeypatch, configured_mail):
    monkeypatch.setattr(MAIL, "send_mail", lambda *args: (_ for _ in ()).throw(OSError("secret")))
    MAIL.notify(tmp_path, FAILURE)
    seen = []
    monkeypatch.setattr(MAIL, "send_mail", lambda config, kind, result, incident: seen.append((kind, incident)))
    assert MAIL.notify(tmp_path, HEALTHY)["status"] == "accepted"
    assert len(seen) == 1 and seen[0][0] == "recovery"
    assert seen[0][1]["checkedAt"] == FAILURE["checkedAt"]


def test_recovery_email_keeps_original_incident_trace_ids(monkeypatch, configured_mail):
    bodies = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *args):
            pass
        def send_message(self, message, **kwargs):
            bodies.append(message.get_content())
        def close(self):
            pass
    monkeypatch.setattr(MAIL.smtplib, "SMTP_SSL", Client)
    MAIL.send_mail(configured_mail | {"port": 465}, "recovery", HEALTHY, FAILURE)
    assert "probe-run-123" in bodies[0] and "probe-run-123-plan" in bodies[0]


def test_failed_recovery_delivery_retries_on_next_healthy_run(tmp_path, monkeypatch, configured_mail):
    monkeypatch.setattr(MAIL, "send_mail", lambda *args: None)
    MAIL.notify(tmp_path, FAILURE)
    monkeypatch.setattr(MAIL, "send_mail", lambda *args: (_ for _ in ()).throw(OSError("secret")))
    assert MAIL.notify(tmp_path, HEALTHY)["status"] == "failed"
    monkeypatch.setattr(MAIL, "send_mail", lambda *args: None)
    assert MAIL.notify(tmp_path, HEALTHY) == {"status": "accepted", "event": "recovery"}
    assert MAIL.notify(tmp_path, HEALTHY)["status"] == "quiet"


def test_missing_optional_mail_credential_leaves_existing_probe_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("PROBE_MAIL_CONFIG", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert MAIL.notify(tmp_path, FAILURE) == {"status": "disabled"}
    assert not (tmp_path / "mail-state.json").exists()


@pytest.mark.parametrize("changes", [{"security": "plain"}, {"to": "a@example.com,b@example.com"},
    {"from": "sender@example.com\r\nBcc: unwanted@example.com"}, {"port": True}, {"password": ""}])
def test_invalid_mail_config_cannot_send(tmp_path, monkeypatch, configured_mail, changes):
    (tmp_path / "mail-config").write_text(json.dumps(configured_mail | changes))
    monkeypatch.setattr(MAIL, "send_mail", lambda *args: pytest.fail("must not send"))
    assert MAIL.notify(tmp_path, FAILURE)["status"] == "failed"


@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_smtp_requires_verified_tls_and_sends_only_safe_fields(monkeypatch, configured_mail, security):
    calls = []
    class Client:
        def __init__(self, host, port, timeout, context=None):
            assert (host, port, timeout) == ("smtp.example.com", 465 if security == "ssl" else 587, 10)
            calls.append("connect")
            if security == "ssl":
                assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        def starttls(self, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append("tls")
        def login(self, username, password):
            assert (username, password) == ("smtp-user", "private-password")
            calls.append("login")
        def send_message(self, message, from_addr, to_addrs):
            assert from_addr == "sender@example.com" and to_addrs == ["recipient@example.com"]
            assert str(message["Subject"]) == "[DayMosaic] AI 规划服务检测异常"
            body = message.get_content()
            assert "MODEL_GATEWAY_ERROR" in body and "502" in body
            assert "probe-run-123" in body and "probe-run-123-plan" in body
            assert "private" not in body
            calls.append("send")
        def close(self):
            calls.append("close")
    monkeypatch.setattr(MAIL.smtplib, "SMTP_SSL", Client)
    monkeypatch.setattr(MAIL.smtplib, "SMTP", Client)
    config = configured_mail | {"security": security, "port": 465 if security == "ssl" else 587}
    MAIL.send_mail(config, "failure", FAILURE | {"rawModelOutput": "private-plan", "token": "private-token"})
    assert calls == (["connect", "tls", "login", "send", "close"] if security == "starttls" else ["connect", "login", "send", "close"])


def test_tls_failure_never_transmits_credentials(monkeypatch, configured_mail):
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def starttls(self, context):
            raise ssl.SSLError("certificate failed")
        def login(self, *args):
            pytest.fail("credentials must not be sent before TLS")
        def close(self):
            pass
    monkeypatch.setattr(MAIL.smtplib, "SMTP", Client)
    with pytest.raises(ssl.SSLError):
        MAIL.send_mail(configured_mail | {"security": "starttls", "port": 587}, "test", {})


def test_probe_persists_real_result_before_email_and_reports_mail_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROBE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROBE_DEVICE_ID", DEVICE)
    http = FakeHTTP()
    monkeypatch.setattr(PROBE, "HTTPClient", lambda base: http)
    def failed_notice(directory, result):
        assert json.loads((directory / "latest.json").read_text())["status"] == "healthy"
        return {"status": "failed", "code": "SMTP_AUTH_FAILED"}
    monkeypatch.setitem(PROBE.NOTIFIER, "notify", failed_notice)
    assert PROBE.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "healthy" and result["notification"]["code"] == "SMTP_AUTH_FAILED"
    assert len([call for call in http.calls if call[0] == "/api/plan/parse"]) == 1
