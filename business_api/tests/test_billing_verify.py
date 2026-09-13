import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from pydantic import SecretStr

import test_appstore_client as tsc
from app.appstore_client import (AppStoreEnvironmentMismatch, AppStoreRejected,
                                 AppStoreServerAPIClient, AppStoreUnavailable)
from app.account_backend import AccountAPISettings, Actor, failure
from app.billing_verify import decrypt_reference, encrypt_reference, verify_apple_purchase

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
PRODUCT = "com.hayden.daymosaic.plus.monthly"
CLAIM_ID = uuid4()
EXPIRES_MS = 1789000000000
EXPIRES_ISO = datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).microsecond // 1000:03d}Z"


@pytest.fixture(scope="module")
def certs():
    # Validity windows follow the real clock so the suite never expires.
    real_now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_certificate = tsc._certificate(tsc._name("Test Root"), tsc._name("Test Root"),
                                        root_key.public_key(), root_key,
                                        real_now - timedelta(days=365),
                                        real_now + timedelta(days=365))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_certificate = tsc._certificate(tsc._name("Apple Test Signer"),
                                        root_certificate.subject,
                                        leaf_key.public_key(), root_key,
                                        real_now - timedelta(days=1),
                                        real_now + timedelta(days=365))
    return tsc.CertChain(root_key=root_key, root_certificate=root_certificate,
                         leaf_key=leaf_key, leaf_certificate=leaf_certificate)


@pytest.fixture
def configuration():
    return AccountAPISettings(supabase_url="https://project.supabase.co",
        supabase_publishable_key="public-key", supabase_service_role_key="server-secret",
        planning_internal_secret="independent-private-planning-secret-with-32-characters",
        time_fragment_token_secret="token-secret-token-secret-token-secret-1234",
        admin_api_key="admin-key-123456", time_fragment_development_device_ids="",
        apple_environment="sandbox", apple_key_id="H26PU75Z9S",
        apple_issuer_id="b24fefbf-30af-43f3-8523-8f2e5c8f8c74",
        store_reference_key=SecretStr(base64.b64encode(os.urandom(32)).decode()))


class FakeApple:
    def __init__(self, *, payload=None, error=None):
        self.payload = payload if payload is not None else {
            "environment": "Sandbox",
            "subscriptionGroupIdentifierItems": [{"subscriptionGroupIdentifier": "group1",
                "subscriptionItems": [{"originalTransactionId": "123", "status": 1,
                                       "expiresDate": EXPIRES_MS}]}]}
        self.error = error
        self.calls = []

    async def subscription_status(self, original_transaction_id):
        self.calls.append(original_transaction_id)
        if self.error:
            raise self.error
        return self.payload


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def close(self):
        pass

    async def billing(self, action, actor, **data):
        self.calls.append((action, actor, data))
        return {"plan": "plus", "status": "active", "validUntil": EXPIRES_ISO,
                "serviceEndAt": None, "entitlementRevision": 1,
                "aiQuota": {"limit": 30, "used": 0, "remaining": 30, "resetsAt": None},
                "billingSources": [{"provider": "apple", "productId": PRODUCT,
                                    "expiresAt": EXPIRES_ISO}]}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _jws_token(certs, *, environment="Sandbox", product=PRODUCT, app_account_token=None,
               original_transaction_id="123"):
    body = {"environment": environment, "productId": product,
            "originalTransactionId": original_transaction_id,
            "appAccountToken": app_account_token or str(uuid4())}
    return tsc._build_jws(body, certs, certs.leaf_certificate)


def _key_p8() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        tsc.Encoding.PEM, tsc.PrivateFormat.PKCS8, tsc.NoEncryption())


def _make_client(certs, *, handler=None):
    return AppStoreServerAPIClient(
        environment="sandbox", key_p8=_key_p8(), key_id="H26PU75Z9S",
        issuer_id="b24fefbf-30af-43f3-8523-8f2e5c8f8c74", bundle_id="com.hayden.timefragment",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler or (
            lambda request: httpx.Response(200, json={"signedPayload": _jws_token(certs)})))),
        pinned_roots=[certs.root_certificate])


def _verify(certs, backend, apple, *, body=None, environment="Sandbox", product=PRODUCT,
            app_account_token=None, original_transaction_id="123", claim_id=None):
    body = body or type("Body", (), {})()
    if getattr(body, "signed_transaction", None) is None:
        body.signed_transaction = _jws_token(certs, environment=environment, product=product,
                                             app_account_token=app_account_token,
                                             original_transaction_id=original_transaction_id)
    if getattr(body, "product_id", None) is None:
        body.product_id = product
    body.claim_id = claim_id if claim_id is not None else getattr(body, "claim_id", None)

    async def run():
        try:
            return await verify_apple_purchase(
                backend=backend, apple_client=apple, settings=configuration_ref[0],
                actor=Actor("account:" + str(uuid4()), str(uuid4())),
                payload_body=body, pinned_roots=[certs.root_certificate])
        finally:
            if apple is not None and hasattr(apple, "aclose"):
                pass
    return asyncio.run(run())


configuration_ref = [None]


@pytest.fixture(autouse=True)
def _capture_configuration(configuration):
    configuration_ref[0] = configuration


def test_verify_success_binds_and_returns_entitlement(certs, configuration):
    apple, backend = FakeApple(), FakeBackend()
    account_token = str(uuid4())
    result = _verify(certs, backend, apple, app_account_token=account_token)
    assert result["plan"] == "plus"
    action, actor, data = backend.calls[0]
    assert action == "apple_verify"
    assert data["originalTransactionId"] == "123"
    assert data["storeStatus"] == "active"
    assert data["expiresAt"] == EXPIRES_ISO
    assert data["appAccountToken"] == account_token
    assert apple.calls == ["123"]


def test_verify_passes_claim_id(certs, configuration):
    backend = FakeBackend()
    _verify(certs, backend, FakeApple(), claim_id=CLAIM_ID)
    assert backend.calls[0][2]["claimId"] == str(CLAIM_ID)


def test_reference_encryption_roundtrip(configuration):
    key = base64.b64decode(configuration.store_reference_key.get_secret_value())
    blob = encrypt_reference(key, "123")
    assert decrypt_reference(key, blob) == "123"
    with pytest.raises(InvalidTag):
        decrypt_reference(os.urandom(32), blob)


def test_product_mismatch_fails(certs, configuration):
    body = type("Body", (), {"signed_transaction": _jws_token(certs, product=PRODUCT),
                             "product_id": "com.hayden.daymosaic.plus.yearly",
                             "claim_id": None})()
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(), body=body)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "PRODUCT_MISMATCH"


def test_product_outside_allowlist_fails(certs, configuration):
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(),
                product="com.hayden.daymosaic.plus.lifetime")
    assert error.value.detail["code"] == "PRODUCT_INVALID"


def test_environment_mismatch_fails(certs, configuration):
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(), environment="Production")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "ENVIRONMENT_MISMATCH"


def test_apple_unavailable_becomes_verification_pending(certs, configuration):
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(error=AppStoreUnavailable("busy")))
    assert error.value.status_code == 202
    assert error.value.detail["code"] == "VERIFICATION_PENDING"


def test_apple_rejected_becomes_verification_failed(certs, configuration):
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(error=AppStoreRejected(404)))
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "VERIFICATION_FAILED"


def test_invalid_jws_fails(certs, configuration):
    body = type("Body", (), {"signed_transaction": "not.a.jws", "product_id": PRODUCT,
                             "claim_id": None})()
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), FakeApple(), body=body)
    assert error.value.detail["code"] == "VERIFICATION_FAILED"


def test_status_without_transaction_fails(certs, configuration):
    apple = FakeApple(payload={"environment": "Sandbox",
                               "subscriptionGroupIdentifierItems": []})
    with pytest.raises(HTTPException) as error:
        _verify(certs, FakeBackend(), apple)
    assert error.value.detail["code"] == "VERIFICATION_FAILED"


def test_expired_purchase_maps_to_expired_status(certs, configuration):
    apple = FakeApple(payload={"environment": "Sandbox",
        "subscriptionGroupIdentifierItems": [{"subscriptionGroupIdentifier": "group1",
            "subscriptionItems": [{"originalTransactionId": "123", "status": 2,
                                   "expiresDate": EXPIRES_MS}]}]})
    backend = FakeBackend()
    _verify(certs, backend, apple)
    assert backend.calls[0][2]["storeStatus"] == "expired"


def test_app_store_client_missing_environment_is_rejected(configuration):
    with pytest.raises(ValueError):
        AppStoreServerAPIClient(environment="staging", key_p8=b"x", key_id="k",
                                issuer_id="i", bundle_id="com.hayden.timefragment")


def test_app_store_client_wraps_transport_errors(certs):
    client = AppStoreServerAPIClient(
        environment="sandbox", key_p8=_key_p8(), key_id="H26PU75Z9S",
        issuer_id="b24fefbf-30af-43f3-8523-8f2e5c8f8c74", bundle_id="com.hayden.timefragment",
        http=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom")))),
        pinned_roots=[certs.root_certificate])

    async def run():
        try:
            await client.subscription_status("123")
        finally:
            await client.aclose()

    with pytest.raises(AppStoreUnavailable):
        asyncio.run(run())


def test_private_key_resolution_follows_path_then_inline(configuration, tmp_path):
    from app.account_backend import resolve_apple_key_p8
    key_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        tsc.Encoding.PEM, tsc.PrivateFormat.PKCS8, tsc.NoEncryption()).decode()
    key_file = tmp_path / "apple.p8"
    key_file.write_text(key_pem)
    settings = configuration.model_copy(update={
        "apple_private_key": None, "apple_private_key_path": str(key_file)})
    assert resolve_apple_key_p8(settings) == key_pem.encode()
    inline = configuration.model_copy(update={
        "apple_private_key_path": "",
        "apple_private_key": SecretStr(key_pem)})
    assert resolve_apple_key_p8(inline) == key_pem.encode()
    assert resolve_apple_key_p8(configuration.model_copy(update={
        "apple_private_key": None, "apple_private_key_path": ""})) is None
