import asyncio
import base64
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore
from app.rent_room_storekit import AppleStoreKit, StoreKitFailure


@pytest.fixture
def settings():
    return RentRoomSettings(_env_file=None, token_secret="native-session-secret-32-characters-long", ios_bundle_id="com.test.xunju",
        storekit_enabled=True, storekit_product_id="com.test.capacity10", storekit_environment="Sandbox",
        storekit_key_id="TESTKEY", storekit_issuer_id=str(uuid.uuid4()), storekit_key_path="not-read-by-injected-provider")


class FakeApple:
    def __init__(self):
        self.transactions = {}; self.calls = []; self.fail = False; self.notice = None
    async def close(self):
        pass
    async def transaction(self, identity):
        self.calls.append(identity)
        if self.fail:
            raise StoreKitFailure()
        return self.transactions[identity].copy()
    async def notification(self, signed):
        if signed != "verified-test-notice":
            raise StoreKitFailure("通知校验失败", 401)
        return self.notice


@pytest.fixture
def service(settings):
    ledger, apple = RentRoomStore(":memory:"), FakeApple()
    with TestClient(create_app(settings, store=ledger, storekit=apple)) as client:
        yield ledger, apple, client


def auth(ledger, account="a" * 64):
    return {"Authorization": "Bearer " + ledger.create_session(account, "", "", provider="phone")}


def transaction(token, identity="10001", version=1000, **changes):
    result = dict(transactionId=identity, environment="Sandbox", productId="com.test.capacity10",
                  appAccountToken=token, signedDate=version, revocationDate=None)
    result.update(changes); return result


def test_disabled_unauthed_and_invalid_requests_never_grant():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        assert client.get("/api/rent-room/ios/storekit/context").status_code == 503
        assert client.post("/api/rent-room/ios/storekit/verify", json={"transactionId": "1"}).status_code == 503
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, storekit_enabled=True)
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, storekit_environment="Xcode")


def test_binding_repeat_purchase_revision_and_legacy_entitlement(service):
    ledger, apple, client = service; headers = auth(ledger)
    first = client.get("/api/rent-room/ios/storekit/context", headers=headers).json()
    second = client.get("/api/rent-room/ios/storekit/context", headers=headers).json()
    assert first == second and uuid.UUID(first["appAccountToken"]).version == 4
    for identity in ["10001", "10002"]:
        apple.transactions[identity] = transaction(first["appAccountToken"], identity)
        result = client.post("/api/rent-room/ios/storekit/verify", headers=headers, json={"transactionId": identity})
        assert result.status_code == 200
    value = result.json(); assert (value["packs"], value["limit"], value["revision"]) == (2, 22, 2)
    again = client.post("/api/rent-room/ios/storekit/verify", headers=headers, json={"transactionId": "10002"}).json()
    assert again == value
    assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 2, "limit": 22}
    assert ledger.packs("a" * 64, "Production") == 0  # Sandbox never enters production entitlement.


def test_other_account_body_spoof_and_concurrent_duplicates(service):
    ledger, apple, client = service; headers = auth(ledger)
    token = client.get("/api/rent-room/ios/storekit/context", headers=headers).json()["appAccountToken"]
    apple.transactions["10001"] = transaction(token)
    other = auth(ledger, "b" * 64)
    assert client.post("/api/rent-room/ios/storekit/verify", headers=other, json={"transactionId": "10001"}).status_code == 403
    assert ledger.packs("b" * 64, "Sandbox") == 0
    for body in [{"transactionId": "../../x"}, {"transactionId": "10001", "packs": 99}, {"transactionId": "10001", "appAccountToken": token}]:
        assert client.post("/api/rent-room/ios/storekit/verify", headers=headers, json=body).status_code == 422
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post("/api/rent-room/ios/storekit/verify", headers=headers, json={"transactionId": "10001"}), range(10)))
    assert all(r.status_code == 200 and r.json()["packs"] == 1 for r in responses)
    assert ledger.db.execute("SELECT count(*) FROM rr_apple_transactions").fetchone()[0] == 1


def test_refresh_failure_retry_refund_late_notice_and_reversal(service):
    ledger, apple, client = service; headers = auth(ledger)
    token = client.get("/api/rent-room/ios/storekit/context", headers=headers).json()["appAccountToken"]
    original = transaction(token); apple.transactions["10001"] = original
    endpoint = "/api/rent-room/ios/storekit/verify"
    assert client.post(endpoint, headers=headers, json={"transactionId": "10001"}).json()["packs"] == 1
    apple.fail = True
    assert client.post("/api/rent-room/ios/storekit/restore", headers=headers).status_code == 503
    assert ledger.packs("a" * 64, "Sandbox") == 1
    apple.fail = False
    assert client.post("/api/rent-room/ios/storekit/restore", headers=headers).json()["packs"] == 1
    apple.transactions["10001"] = transaction(token, version=2000, revocationDate=1999)
    apple.notice = str(uuid.uuid4()), original  # Late purchase delivery must query current Apple state.
    for _ in range(2):
        assert client.post("/api/rent-room/ios/storekit/notifications", json={"signedPayload": "verified-test-notice"}).status_code == 200
    assert ledger.packs("a" * 64, "Sandbox") == 0
    assert ledger.db.execute("SELECT count(*) FROM rr_apple_events").fetchone()[0] == 1
    assert client.post(endpoint, headers=headers, json={"transactionId": "10001"}).json()["status"] == "revoked"
    assert ledger.apply_apple("a" * 64, original)["packs"] == 0
    apple.transactions["10001"] = transaction(token, version=3000)  # Authoritative refund reversal.
    restored = client.post("/api/rent-room/ios/storekit/restore", headers=headers).json()
    assert restored["packs"] == 1 and restored["revision"] == 3


def test_unknown_notice_invalid_signature_and_disabled_purchase_recovery(service, settings):
    ledger, apple, client = service; headers = auth(ledger)
    token = client.get("/api/rent-room/ios/storekit/context", headers=headers).json()["appAccountToken"]
    apple.transactions["10001"] = transaction(token)
    assert client.post("/api/rent-room/ios/storekit/notifications", json={"signedPayload": "forged"}).status_code == 401
    apple.notice = str(uuid.uuid4()), transaction(str(uuid.uuid4()))
    assert client.post("/api/rent-room/ios/storekit/notifications", json={"signedPayload": "verified-test-notice"}).json() == {"received": True}
    assert apple.calls == []
    settings.storekit_enabled = False
    assert client.get("/api/rent-room/ios/storekit/context", headers=headers).json()["available"] is False
    # Disabling new sales must not strand an already paid transaction.
    assert client.post("/api/rent-room/ios/storekit/verify", headers=headers, json={"transactionId": "10001"}).json()["packs"] == 1


def test_ledger_migration_persists_binding_versions_and_cross_channel_packs(tmp_path):
    path = str(tmp_path / "ledger.sqlite"); store = RentRoomStore(path); account = "a" * 64
    token = store.apple_token(account)
    store.apply_apple(account, transaction(token, environment="Production"))
    session = {"account": account, "openid": "wx-user"}; order = store.create_order(session, "request-123")
    store.apply(order["id"], "paid", "wx-payment")
    before = store.apple_snapshot(account, "Production"); assert before["packs"] == 2
    store.close(); store = RentRoomStore(path)
    assert store.apple_token(account) == token and store.apple_snapshot(account, "Production") == before
    store.apply(order["id"], "refunded")
    assert store.apple_snapshot(account, "Production")["packs"] == 1
    assert store.apple_snapshot(account, "Production")["revision"] > before["revision"]
    store.close()


@pytest.fixture
def signed_chain(tmp_path, settings):
    now = datetime.now(timezone.utc)
    keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(3)]
    names = [x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]) for name in ["test-root", "test-intermediate", "test-leaf"]]
    certs = []
    for index in range(3):
        issuer = max(0, index - 1)
        builder = (x509.CertificateBuilder().subject_name(names[index]).issuer_name(names[issuer]).public_key(keys[index].public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=index < 2, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, index < 2, index < 2, False, False), True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(keys[index].public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(keys[issuer].public_key()), False))
        if index:
            builder = builder.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier("1.2.840.113635.100.6.2.1" if index == 1 else "1.2.840.113635.100.6.11.1"), b"\x05\x00"), False)
        certs.append(builder.sign(keys[issuer], hashes.SHA256()))
    keyfile = tmp_path / "service-test.p8"
    keyfile.write_bytes(keys[0].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    settings.storekit_key_path = str(keyfile)
    root = certs[0].public_bytes(serialization.Encoding.DER)
    verifier = SignedDataVerifier([root], False, Environment.SANDBOX, settings.ios_bundle_id)
    headers = {"x5c": [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in reversed(certs)]}
    def sign(data): return jwt.encode(data, keys[2], algorithm="ES256", headers=headers)
    return verifier, sign


@pytest.mark.parametrize("changes", [{"productId": "other"}, {"type": "Non-Consumable"}, {"quantity": 2}, {"quantity": True},
    {"appAccountToken": None}, {"environment": "Xcode"}, {"inAppOwnershipType": "FAMILY_SHARED"}, {"bundleId": "other"},
    {"signedDate": -1}, {"signedDate": True}, {"revocationDate": True}])
def test_verified_payload_still_requires_exact_product_ownership_and_environment(settings, changes):
    provider = AppleStoreKit(settings.model_copy(update={"storekit_key_path": "", "storekit_enabled": False}))
    data = dict(transactionId="10001", bundleId=settings.ios_bundle_id, productId=settings.storekit_product_id,
        appAccountToken=str(uuid.uuid4()), environment="Sandbox", type="Consumable", inAppOwnershipType="PURCHASED",
        quantity=1, signedDate=int(time.time()*1000), purchaseDate=int(time.time()*1000), revocationDate=None)
    data.update(changes)
    with pytest.raises(StoreKitFailure): provider.validate(SimpleNamespace(**data))
    asyncio.run(provider.close())


def test_real_official_verifier_and_canonical_apple_http_reject_tampering(settings, signed_chain):
    verifier, sign = signed_chain; now = int(time.time()*1000)
    claims = dict(transactionId="10001", originalTransactionId="10001", bundleId=settings.ios_bundle_id,
        productId=settings.storekit_product_id, appAccountToken=str(uuid.uuid4()), environment="Sandbox", type="Consumable",
        inAppOwnershipType="PURCHASED", quantity=1, signedDate=now, purchaseDate=now)
    signed = sign(claims); replies = [signed, signed[:-8] + "AAAAAAAA", sign({**claims, "bundleId": "wrong"})]
    def handler(request):
        assert request.url.host == "api.storekit-sandbox.apple.com"
        assert request.url.path == "/inApps/v1/transactions/10001"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"signedTransactionInfo": replies.pop(0)})
    async def run():
        provider = AppleStoreKit(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), verifier=verifier)
        result = await provider.transaction("10001"); assert result["appAccountToken"] == claims["appAccountToken"]
        for _ in range(2):
            with pytest.raises(StoreKitFailure): await provider.transaction("10001")
        await provider.close()
    asyncio.run(run())


def test_real_official_notification_requires_outer_and_inner_signatures(settings, signed_chain):
    verifier, sign = signed_chain
    now = int(time.time() * 1000)
    token = str(uuid.uuid4())
    claims = dict(transactionId="10001", originalTransactionId="10001", bundleId=settings.ios_bundle_id,
        productId=settings.storekit_product_id, appAccountToken=token, environment="Sandbox", type="Consumable",
        inAppOwnershipType="PURCHASED", quantity=1, signedDate=now, purchaseDate=now)
    identity = str(uuid.uuid4())
    notice = dict(notificationType="ONE_TIME_CHARGE", notificationUUID=identity, version="2.0", signedDate=now,
        data=dict(bundleId=settings.ios_bundle_id, environment="Sandbox", signedTransactionInfo=sign(claims)))
    async def run():
        provider = AppleStoreKit(settings, verifier=verifier)
        result_id, result = await provider.notification(sign(notice))
        assert result_id == identity and result["appAccountToken"] == token
        for payload in [sign(notice)[:-8] + "AAAAAAAA", sign({**notice, "data": {**notice["data"], "signedTransactionInfo": "forged"}})]:
            with pytest.raises(StoreKitFailure) as error:
                await provider.notification(payload)
            assert error.value.status == 401
        await provider.close()
    asyncio.run(run())
