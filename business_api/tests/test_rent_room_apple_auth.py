import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.rent_room_apple_auth import AppleAuthError, AppleChallenges, AppleIdentity
from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore


@pytest.fixture
def settings():
    return RentRoomSettings(_env_file=None, apple_login_enabled=True,
                            token_secret="apple-test-session-secret-32-characters",
                            ios_bundle_id="com.keeline.xunju.ios.dev.iosparity20261019", free_capacity=4)


@pytest.fixture(scope="module")
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="apple-test", alg="RS256", use="sig")
    return key, jwk


def provider(settings, signing, handler=None):
    return AppleIdentity(settings.ios_bundle_id, httpx.AsyncClient(transport=httpx.MockTransport(
        handler or (lambda _: httpx.Response(200, json={"keys": [signing[1]]})))))


def signed_token(settings, signing, nonce, changes=None, omitted=()):
    now = int(time.time())
    claims = {"iss": "https://appleid.apple.com", "aud": settings.ios_bundle_id, "sub": "apple-user-1",
              "iat": now, "exp": now + 300, "nonce": nonce, "email": "same@example.com"}
    claims.update(changes or {})
    for key in omitted:
        del claims[key]
    return jwt.encode(claims, signing[0], algorithm="RS256", headers={"kid": "apple-test"})


def challenge(client):
    result = client.post("/api/rent-room/auth/apple/challenge", json={})
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["expiresIn"] == 300
    return result.json()


def login(client, settings, signing, request=None, **kwargs):
    request = request or challenge(client)
    return client.post("/api/rent-room/auth/apple/session", json={
        "challengeId": request["challengeId"],
        "identityToken": signed_token(settings, signing, request["nonce"], **kwargs)},
        headers={"x-rent-room-capacity-version": "2"})


def test_default_disabled_and_incomplete_configuration_fail_closed():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        assert client.post("/api/rent-room/auth/apple/challenge").status_code == 503
        assert client.post("/api/rent-room/auth/phone/code", json={}).status_code == 404
    for fields in ({}, {"ios_bundle_id": "app"}, {"token_secret": "x" * 32},
                   {"ios_bundle_id": "  ", "token_secret": "x" * 32}):
        with pytest.raises(ValueError):
            RentRoomSettings(_env_file=None, apple_login_enabled=True, **fields)


def test_apple_session_v2_capacity_stable_account_and_revocation(settings, signing):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, apple=provider(settings, signing))) as client:
        request = challenge(client)
        response = login(client, settings, signing, request)
        assert response.status_code == 200
        result = response.json()
        assert result["provider"] == "apple" and result["expiresIn"] == 7200
        assert result["capacity"] == {"version": 2, "freeLimit": 4}
        assert (result["packs"], result["limit"]) == (0, 4)
        assert login(client, settings, signing, request).status_code == 401
        again = login(client, settings, signing).json()
        assert again["account"] == result["account"] and again["token"] != result["token"]
        headers = {"Authorization": "Bearer " + result["token"], "x-rent-room-capacity-version": "2"}
        assert client.get("/api/rent-room/entitlements", headers=headers).json()["limit"] == 4
        assert ledger.session(result["token"])["provider"] == "apple"
        assert client.post("/api/rent-room/session/revoke", headers=headers).json() == {"revoked": True}
        assert client.get("/api/rent-room/entitlements", headers=headers).status_code == 401
        assert ledger.session(again["token"]) is not None
        assert ledger.db.execute("SELECT count(*) FROM rr_session_providers").fetchone()[0] == 1
        assert "same@example.com" not in " ".join(ledger.db.iterdump())


@pytest.mark.parametrize("changes", [
    {"aud": "com.keeline.xunju.ios"}, {"aud": ["com.keeline.xunju.ios.dev.iosparity20261019"]},
    {"iss": "https://attacker.example"}, {"exp": 1}, {"iat": int(time.time()) + 3600},
    {"iat": "100"}, {"exp": True}, {"nonce": ""}, {"nonce": 2}, {"sub": ""}, {"sub": 2},
])
def test_invalid_claims_never_issue_session(settings, signing, changes):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, apple=provider(settings, signing))) as client:
        response = login(client, settings, signing, changes=changes)
        assert response.status_code == 401
        assert ledger.db.execute("SELECT count(*) FROM rr_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("omitted", ["sub", "iss", "aud", "exp", "iat", "nonce"])
def test_required_claims(settings, signing, omitted):
    with TestClient(create_app(settings, apple=provider(settings, signing))) as client:
        assert login(client, settings, signing, omitted=(omitted,)).status_code == 401


def test_nonce_mismatch_expiry_and_email_account_isolation(settings, signing):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, apple=provider(settings, signing))) as client:
        request = challenge(client)
        assert login(client, settings, signing, request, changes={"nonce": "wrong-nonce-123456789"}).status_code == 401
        first = login(client, settings, signing, request).json()
        second = login(client, settings, signing, changes={"sub": "another-apple-user"}).json()
        assert first["account"] != second["account"]
        request = challenge(client)
        ledger.db.execute("UPDATE rr_apple_challenges SET expires=0")
        ledger.db.commit()
        assert login(client, settings, signing, request).status_code == 401


@pytest.mark.parametrize("attack", ["tamper", "unknown-key", "HS256", "none"])
def test_signature_and_algorithm_fail_closed(settings, signing, attack):
    with TestClient(create_app(settings, apple=provider(settings, signing))) as client:
        request = challenge(client)
        token = signed_token(settings, signing, request["nonce"])
        claims = jwt.decode(token, options={"verify_signature": False})
        if attack == "tamper":
            parts = token.split(".")
            parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
            token = ".".join(parts)
        elif attack == "unknown-key":
            token = jwt.encode(claims, signing[0], algorithm="RS256", headers={"kid": "unknown"})
        else:
            token = jwt.encode(claims, "x" * 32 if attack == "HS256" else None,
                               algorithm=attack, headers={"kid": "apple-test"})
        assert client.post("/api/rent-room/auth/apple/session", json={
            "challengeId": request["challengeId"], "identityToken": token}).status_code == 401


@pytest.mark.parametrize("payload", [[], {"keys": []}, {"keys": ["invalid-key"]}, {"keys": None}])
def test_invalid_jwks_fail_closed(settings, signing, payload):
    apple = provider(settings, signing, lambda _: httpx.Response(200, json=payload))
    with TestClient(create_app(settings, apple=apple)) as client:
        assert login(client, settings, signing).status_code == 401


def test_jwks_outage_retry_and_sensitive_input_redaction(settings, signing):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://appleid.apple.com/auth/keys"
        if len(calls) == 1:
            raise httpx.ConnectError("private-upstream-error", request=request)
        return httpx.Response(200, json={"keys": [signing[1]]})
    with TestClient(create_app(settings, apple=provider(settings, signing, handler))) as client:
        request = challenge(client)
        failed = login(client, settings, signing, request)
        assert failed.status_code == 503 and "private-upstream-error" not in failed.text
        assert login(client, settings, signing, request).status_code == 200
        secret = "private-token-input"
        response = client.post("/api/rent-room/auth/apple/session", json={
            "challengeId": "bad", "identityToken": secret})
        assert response.status_code == 422 and secret not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_jwks_cache_rotation_and_unknown_key_refresh_throttling(settings, signing):
    calls = []
    def handler(request):
        calls.append(request)
        keys = [signing[1]]
        if len(calls) > 1:
            keys.append({**signing[1], "kid": "rotated"})
        return httpx.Response(200, json={"keys": keys})
    apple = provider(settings, signing, handler)
    async def run():
        token = signed_token(settings, signing, "test-nonce-123456789")
        assert len(await asyncio.gather(*(apple.verify(token) for _ in range(5)))) == 5
        assert len(calls) == 1
        claims = jwt.decode(token, options={"verify_signature": False})
        token = jwt.encode(claims, signing[0], algorithm="RS256", headers={"kid": "rotated"})
        with pytest.raises(AppleAuthError):
            await apple.verify(token)
        assert len(calls) == 1
        apple.refreshed -= 61
        assert (await apple.verify(token))["sub"] == claims["sub"]
        assert len(calls) == 2
        await apple.close()
    asyncio.run(run())


def test_rate_limit_survives_restart_and_uses_last_proxy_hop(settings, tmp_path):
    settings.db_path = str(tmp_path / "rate.sqlite")
    with TestClient(create_app(settings)) as client:
        for i in range(20):
            assert client.post("/api/rent-room/auth/apple/challenge", headers={
                "x-forwarded-for": f"spoof-{i}, 192.0.2.1"}).status_code == 200
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/rent-room/auth/apple/challenge", headers={
            "x-forwarded-for": "new-spoof, 192.0.2.1"}).status_code == 429


def test_concurrent_challenge_consumption_across_connections(tmp_path):
    path = str(tmp_path / "concurrent.sqlite")
    stores = [RentRoomStore(path), RentRoomStore(path)]
    challenges = [AppleChallenges(store) for store in stores]
    request = challenges[0].create()
    def consume(service):
        try:
            service.consume(request["challengeId"], request["nonce"])
            return "ok"
        except AppleAuthError:
            return "replay"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, challenges)) == ["ok", "replay"]
    for store in stores:
        store.close()


def test_old_wechat_rows_and_old_version_writes_remain_compatible(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE rr_sessions(token_hash TEXT PRIMARY KEY,account TEXT NOT NULL,openid TEXT NOT NULL,session_key TEXT NOT NULL,expires INTEGER NOT NULL)")
    db.execute("INSERT INTO rr_sessions VALUES (?,?,?,?,?)", (
        hashlib.sha256(b"legacy").hexdigest(), "original-account", "original-openid", "encrypted-key", int(time.time()) + 7200))
    db.commit()
    ledger = RentRoomStore(path)
    old = ledger.session("legacy")
    assert old["provider"] == "wechat"
    assert (old["account"], old["openid"], old["session_key"]) == ("original-account", "original-openid", "encrypted-key")
    ledger.create_session("apple-account", "", "", provider="apple")
    # Rolling back the image must not break its five-column INSERT statement.
    db.execute("INSERT INTO rr_sessions VALUES (?,?,?,?,?)", (
        hashlib.sha256(b"old-writer").hexdigest(), "old-account", "old-openid", "old-key", int(time.time()) + 7200))
    db.commit()
    assert ledger.session("old-writer")["provider"] == "wechat"
    ledger.close()
    db.close()


def test_apple_cannot_buy_wechat_orders_or_share_a_wechat_account(settings, signing):
    from test_rent_room_capacity import FakeWeChat, settings as wechat_settings
    # Use the existing payment configuration so rejection is provider-specific.
    values = wechat_settings.__wrapped__().model_dump()
    values.update(apple_login_enabled=True, ios_bundle_id=settings.ios_bundle_id)
    configured = RentRoomSettings(**values)
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(configured, store=ledger, provider=FakeWeChat(configured),
                               apple=provider(configured, signing))) as client:
        native = login(client, configured, signing).json()
        wechat = client.post("/api/rent-room/session", json={"code": "A"}).json()
        assert native["account"] != wechat["account"]
        response = client.post("/api/rent-room/orders", headers={"Authorization": "Bearer " + native["token"]},
            json={"requestId": "native-order-001", "productId": "room_capacity_10", "platform": "android"})
        assert response.status_code == 503 and "payment" not in response.json()
        assert ledger.db.execute("SELECT count(*) FROM rr_orders").fetchone()[0] == 0
