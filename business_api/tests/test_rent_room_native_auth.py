import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.rent_room_main import create_app
from app.rent_room_native_auth import AliyunSMS, AppleIdentity, NativeAuthError
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore


@pytest.fixture
def settings():
    return RentRoomSettings(_env_file=None, token_secret="native-session-secret-32-characters-long",
        auth_identity_secret="independent-stable-identity-secret-32-chars", sms_login_enabled=True,
        sms_access_key_id="test-key", sms_access_key_secret="test-key-secret",
        sms_sign_name="测试签名", sms_template_code="SMS_TEST", apple_login_enabled=True,
        ios_bundle_id="com.example.xunju")


class FakeSMS:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, phone, code):
        if self.fail:
            raise NativeAuthError("验证码发送失败，请稍后重试", 503)
        self.sent.append((phone, code))


@pytest.fixture
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="apple-test", alg="RS256", use="sig")
    return key, jwk


def apple_provider(settings, signing):
    _, key = signing
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [key]}))
    return AppleIdentity(settings.ios_bundle_id, httpx.AsyncClient(transport=transport))


def signed_token(settings, signing, request_nonce, **changes):
    now = int(time.time())
    claims = {"iss": "https://appleid.apple.com", "aud": settings.ios_bundle_id, "sub": "apple-user-1",
              "iat": now, "exp": now + 300, "nonce": request_nonce, "email": "same@example.com"}
    claims.update(changes)
    return jwt.encode(claims, signing[0], algorithm="RS256", headers={"kid": "apple-test"})


def send_code(client, phone="13800138000", **kwargs):
    return client.post("/api/rent-room/auth/phone/code", json={"phone": phone}, **kwargs)


def phone_login(client, sms, challenge, phone="13800138000", code=None):
    return client.post("/api/rent-room/auth/phone/session", json={"phone": phone,
        "challengeId": challenge, "code": code or sms.sent[-1][1]})


def test_native_auth_is_disabled_without_configuration_and_rejects_invalid_inputs():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        assert send_code(client).status_code == 503
        assert client.post("/api/rent-room/auth/apple/challenge").status_code == 503
        for phone in ("", "12800138000", "13800138000\n", "+8613800138000", "１３８００１３８０００"):
            assert send_code(client, phone).status_code == 422
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, sms_login_enabled=True)
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, apple_login_enabled=True)


def test_phone_session_is_stable_private_single_use_and_revocable(settings):
    ledger, sms = RentRoomStore(":memory:"), FakeSMS()
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        response = send_code(client)
        assert response.status_code == 200
        challenge = response.json()["challengeId"]
        assert response.json()["retryAfter"] == 60
        assert "code" not in response.json()
        row = ledger.db.execute("SELECT * FROM rr_auth_challenges").fetchone()
        assert "13800138000" not in json.dumps(dict(row))
        assert sms.sent[0][1] not in row["digest"]
        login = phone_login(client, sms, challenge)
        assert login.status_code == 200
        account = login.json()["account"]
        assert login.json()["provider"] == "phone"
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 0, "limit": 2}
        assert phone_login(client, sms, challenge).status_code == 401
        # After a later SMS login the same phone owns the same account.
        ledger.db.execute("UPDATE rr_auth_challenges SET created=created-61")
        ledger.db.commit()
        second = phone_login(client, sms, send_code(client).json()["challengeId"])
        assert second.json()["account"] == account
        assert second.json()["token"] != login.json()["token"]
        assert client.post("/api/rent-room/session/revoke", headers=headers).json() == {"revoked": True}
        assert client.get("/api/rent-room/entitlements", headers=headers).status_code == 401
        assert ledger.db.execute("SELECT provider FROM rr_sessions").fetchone()[0] == "phone"


def test_sms_cooldown_wrong_code_limit_and_expiry(settings):
    ledger, sms = RentRoomStore(":memory:"), FakeSMS()
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        challenge = send_code(client).json()["challengeId"]
        assert send_code(client).status_code == 429
        wrong = "999999" if sms.sent[-1][1] != "999999" else "000000"
        for _ in range(5):
            assert phone_login(client, sms, challenge, code=wrong).status_code == 401
        assert phone_login(client, sms, challenge).status_code == 401
        assert ledger.db.execute("SELECT attempts FROM rr_auth_challenges").fetchone()[0] == 5
        challenge = send_code(client, "13900139000").json()["challengeId"]
        ledger.db.execute("UPDATE rr_auth_challenges SET expires=? WHERE id=?", (int(time.time()) - 1, challenge))
        ledger.db.commit()
        assert phone_login(client, sms, challenge, "13900139000").status_code == 401
        assert ledger.db.execute("SELECT count(*) FROM rr_sessions").fetchone()[0] == 0


def test_sms_failure_never_activates_code_and_ip_limit_is_durable(settings, tmp_path):
    path, sms = str(tmp_path / "auth.sqlite"), FakeSMS()
    ledger = RentRoomStore(path)
    sms.fail = True
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        assert send_code(client).status_code == 503
        assert ledger.db.execute("SELECT ready FROM rr_auth_challenges").fetchone()[0] == 0
        sms.fail = False
        for i in range(19):
            assert send_code(client, "139%08d" % i).status_code == 200
        assert send_code(client, "13700137000").status_code == 429
    ledger = RentRoomStore(path)
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        assert send_code(client, "13700137000").status_code == 429


def test_daily_sms_budget_and_different_phone_cannot_use_same_challenge(settings):
    ledger, sms = RentRoomStore(":memory:"), FakeSMS()
    settings.sms_daily_limit = 2
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        challenge = send_code(client).json()["challengeId"]
        assert phone_login(client, sms, challenge, "13900139000").status_code == 401
        assert phone_login(client, sms, challenge).status_code == 200
        assert send_code(client, "13900139000").status_code == 200
        assert send_code(client, "13700137000").status_code == 429
        assert len(sms.sent) == 2


def test_concurrent_sms_code_can_create_only_one_session(settings):
    ledger, sms = RentRoomStore(":memory:"), FakeSMS()
    with TestClient(create_app(settings, store=ledger, sms=sms)) as client:
        challenge = send_code(client).json()["challengeId"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: phone_login(client, sms, challenge).status_code, range(2)))
        assert sorted(results) == [200, 401]
        assert ledger.db.execute("SELECT count(*) FROM rr_sessions").fetchone()[0] == 1


def test_apple_signature_nonce_replay_and_account_isolation(settings, signing):
    ledger, sms = RentRoomStore(":memory:"), FakeSMS()
    with TestClient(create_app(settings, store=ledger, sms=sms, apple=apple_provider(settings, signing))) as client:
        challenge = client.post("/api/rent-room/auth/apple/challenge").json()
        token = signed_token(settings, signing, challenge["nonce"])
        body = {"challengeId": challenge["challengeId"], "identityToken": token}
        result = client.post("/api/rent-room/auth/apple/session", json=body)
        assert result.status_code == 200
        assert result.json()["provider"] == "apple"
        assert client.post("/api/rent-room/auth/apple/session", json=body).status_code == 401
        headers = {"Authorization": "Bearer " + result.json()["token"]}
        assert client.get("/api/rent-room/entitlements", headers=headers).status_code == 200
        another = client.post("/api/rent-room/auth/apple/challenge").json()
        wrong = {"challengeId": another["challengeId"], "identityToken": token}
        assert client.post("/api/rent-room/auth/apple/session", json=wrong).status_code == 401
        # Identical email never merges a different Apple subject or phone account.
        wrong["identityToken"] = signed_token(settings, signing, another["nonce"], sub="apple-user-2")
        second = client.post("/api/rent-room/auth/apple/session", json=wrong)
        assert second.status_code == 200
        assert second.json()["account"] != result.json()["account"]
        phone = phone_login(client, sms, send_code(client).json()["challengeId"])
        assert phone.json()["account"] not in (result.json()["account"], second.json()["account"])


@pytest.mark.parametrize("changes", [
    {"aud": "other.app"}, {"iss": "https://attacker.example"}, {"exp": 1},
    {"iat": int(time.time()) + 3600}, {"nonce": ""}, {"sub": ""},
])
def test_apple_rejects_invalid_claims(settings, signing, changes):
    with TestClient(create_app(settings, apple=apple_provider(settings, signing))) as client:
        challenge = client.post("/api/rent-room/auth/apple/challenge").json()
        token = signed_token(settings, signing, challenge["nonce"], **changes)
        result = client.post("/api/rent-room/auth/apple/session", json={"challengeId": challenge["challengeId"], "identityToken": token})
        assert result.status_code == 401
        assert "identityToken" not in result.text


def test_apple_rejects_tampered_signature_and_unknown_key(settings, signing):
    with TestClient(create_app(settings, apple=apple_provider(settings, signing))) as client:
        challenge = client.post("/api/rent-room/auth/apple/challenge").json()
        token = signed_token(settings, signing, challenge["nonce"])
        parts = token.split(".")
        signature = parts[2]
        parts[2] = ("A" if signature[0] != "A" else "B") + signature[1:]
        body = {"challengeId": challenge["challengeId"], "identityToken": ".".join(parts)}
        assert client.post("/api/rent-room/auth/apple/session", json=body).status_code == 401
        body["identityToken"] = jwt.encode(jwt.decode(token, options={"verify_signature": False}), signing[0], algorithm="RS256", headers={"kid": "unknown"})
        assert client.post("/api/rent-room/auth/apple/session", json=body).status_code == 401


def test_official_sms_sdk_uses_configured_template_without_retry(settings, monkeypatch):
    from alibabacloud_dysmsapi20170525.client import Client
    from alibabacloud_dysmsapi20170525.models import SendSmsResponse, SendSmsResponseBody
    import asyncio
    seen = []

    async def send(self, request, runtime):
        seen.append((request, runtime))
        return SendSmsResponse(body=SendSmsResponseBody(code="OK"))

    monkeypatch.setattr(Client, "send_sms_with_options_async", send)
    asyncio.run(AliyunSMS(settings).send("13800138000", "012345"))
    request, runtime = seen[0]
    assert request.phone_numbers == "13800138000"
    assert request.sign_name == settings.sms_sign_name
    assert request.template_code == settings.sms_template_code
    assert json.loads(request.template_param) == {"code": "012345"}
    assert runtime.autoretry is False


def test_failed_sms_sdk_response_never_leaks_upstream_secrets(settings, monkeypatch):
    from alibabacloud_dysmsapi20170525.client import Client
    from alibabacloud_dysmsapi20170525.models import SendSmsResponse, SendSmsResponseBody
    import asyncio

    async def reject(self, request, runtime):
        return SendSmsResponse(body=SendSmsResponseBody(code="isv.BUSINESS_LIMIT_CONTROL"))

    monkeypatch.setattr(Client, "send_sms_with_options_async", reject)
    with pytest.raises(NativeAuthError) as error:
        asyncio.run(AliyunSMS(settings).send("13800138000", "012345"))
    assert error.value.status == 503

    async def fail(self, request, runtime):
        raise RuntimeError("secret-key-in-signed-provider-url")

    monkeypatch.setattr(Client, "send_sms_with_options_async", fail)
    with pytest.raises(NativeAuthError) as error:
        asyncio.run(AliyunSMS(settings).send("13800138000", "012345"))
    assert "secret-key" not in str(error.value)


def test_apple_expired_challenge_and_missing_nonce_fail(settings, signing):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, apple=apple_provider(settings, signing))) as client:
        challenge = client.post("/api/rent-room/auth/apple/challenge").json()
        token = signed_token(settings, signing, challenge["nonce"])
        claims = jwt.decode(token, options={"verify_signature": False})
        del claims["nonce"]
        body = {"challengeId": challenge["challengeId"], "identityToken": jwt.encode(claims, signing[0], algorithm="RS256", headers={"kid": "apple-test"})}
        assert client.post("/api/rent-room/auth/apple/session", json=body).status_code == 401
        ledger.db.execute("UPDATE rr_auth_challenges SET expires=0")
        ledger.db.commit()
        body["identityToken"] = token
        assert client.post("/api/rent-room/auth/apple/session", json=body).status_code == 401
        assert ledger.db.execute("SELECT count(*) FROM rr_sessions").fetchone()[0] == 0


def test_native_sessions_cannot_request_wechat_payment(settings):
    from test_rent_room_capacity import FakeWeChat
    import base64
    values = settings.model_dump()
    values.update(app_secret="test-app-secret", app_key="test-payment-key", push_token="test-push-token",
                  push_aes_key=base64.b64encode(b"a" * 32).decode().rstrip("="),
                  payments_enabled=True, ios_payments_enabled=True)
    settings = RentRoomSettings(**values)
    sms = FakeSMS()
    with TestClient(create_app(settings, sms=sms, provider=FakeWeChat(settings))) as client:
        token = phone_login(client, sms, send_code(client).json()["challengeId"]).json()["token"]
        response = client.post("/api/rent-room/orders", headers={"Authorization": "Bearer " + token},
            json={"requestId": "native-order-001", "productId": "room_capacity_10", "platform": "ios"})
        assert response.status_code == 503
        assert "payment" not in response.json()
        assert "iOS" in response.json()["detail"]["message"]


def test_existing_wechat_sessions_migrate_without_rebinding(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.sqlite")
    token = "legacy-session-token"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE rr_sessions(token_hash TEXT PRIMARY KEY,account TEXT NOT NULL,openid TEXT NOT NULL,session_key TEXT NOT NULL,expires INTEGER NOT NULL)")
    db.execute("INSERT INTO rr_sessions VALUES (?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), "legacy-account", "original-openid", "encrypted-original-key", int(time.time()) + 3600))
    db.commit(); db.close()
    ledger = RentRoomStore(path)
    row = ledger.session(token)
    assert row["provider"] == "wechat"
    assert (row["account"], row["openid"], row["session_key"]) == ("legacy-account", "original-openid", "encrypted-original-key")
    ledger.close()


@pytest.mark.parametrize("payload", [[], {"keys": ["invalid-key"]}, {"keys": None}])
def test_apple_invalid_key_response_fails_closed(settings, signing, payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    apple = AppleIdentity(settings.ios_bundle_id, httpx.AsyncClient(transport=transport))
    with TestClient(create_app(settings, apple=apple)) as client:
        challenge = client.post("/api/rent-room/auth/apple/challenge").json()
        response = client.post("/api/rent-room/auth/apple/session", json={"challengeId": challenge["challengeId"],
            "identityToken": signed_token(settings, signing, challenge["nonce"])})
        assert response.status_code == 401
