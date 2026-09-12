import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

import test_appstore_client as tsc
from app.account_api import create_account_api
from app.account_backend import AccountAPISettings, Actor, failure
from app.billing_verify import decrypt_reference, encrypt_reference
from app.billing_worker import (
    handle_apple_webhook,
    process_notification,
    process_pending_events,
    reconcile,
)
from app.appstore_client import AppStoreUnavailable

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
PRODUCT = "com.hayden.daymosaic.plus.monthly"
EXPIRES_MS = 1789000000000
EXPIRES_ISO = datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%S.") + f"{datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).microsecond // 1000:03d}Z"
ACCOUNT_ID = uuid4()


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
        store_reference_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        apple_private_key=SecretStr(ec.generate_private_key(ec.SECP256R1()).private_bytes(
            tsc.Encoding.PEM, tsc.PrivateFormat.PKCS8, tsc.NoEncryption()).decode()))


class FakeApple:
    def __init__(self, *, payload=None, error=None, pinned_roots=None):
        self.payload = payload if payload is not None else {
            "environment": "Sandbox",
            "subscriptionGroupIdentifierItems": [{"subscriptionGroupIdentifier": "group1",
                "subscriptionItems": [{"originalTransactionId": "123", "status": 1,
                                       "expiresDate": EXPIRES_MS}]}]}
        self.error = error
        self.pinned_roots = pinned_roots
        self.calls = []

    async def subscription_status(self, original_transaction_id):
        self.calls.append(original_transaction_id)
        if self.error:
            raise self.error
        return self.payload

    async def aclose(self):
        pass


class WorkerBackend:
    def __init__(self, *, event_receive=None, account_by_token=None,
                 reconcile_chains=None, event_pending=None):
        self.calls = []
        self._event_receive = event_receive if event_receive is not None else {"received": True}
        self._account_by_token = account_by_token or {"userID": str(ACCOUNT_ID)}
        self._reconcile_chains = reconcile_chains or []
        self._event_pending = event_pending or {"events": []}

    async def close(self):
        pass

    async def billing_event(self, action, **data):
        self.calls.append((action, data))
        if action == "event_receive":
            if isinstance(self._event_receive, Exception):
                raise self._event_receive
            return self._event_receive
        if action == "account_by_token":
            if isinstance(self._account_by_token, Exception):
                raise self._account_by_token
            return self._account_by_token
        if action == "reconcile_list":
            return {"chains": self._reconcile_chains}
        if action == "event_pending":
            return self._event_pending
        return {"updated": 1}

    async def billing(self, action, actor=None, **data):
        self.calls.append((action, actor, data))
        return {"plan": "plus", "status": "active", "validUntil": EXPIRES_ISO,
                "serviceEndAt": None, "entitlementRevision": 2,
                "aiQuota": {"limit": 30, "used": 0, "remaining": 30, "resetsAt": None},
                "billingSources": [{"provider": "apple", "productId": PRODUCT,
                                    "expiresAt": EXPIRES_ISO}]}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _encrypted_reference(configuration, reference: str) -> str:
    key = base64.b64decode(configuration.store_reference_key.get_secret_value())
    return encrypt_reference(key, reference)


def _notification_token(certs, *, notification_uuid=None, environment="Sandbox",
                        product=PRODUCT, app_account_token=None,
                        original_transaction_id="123"):
    transaction_body = {"originalTransactionId": original_transaction_id,
                        "productId": product,
                        "appAccountToken": app_account_token or str(uuid4()),
                        "environment": environment}
    transaction_jws = tsc._build_jws(transaction_body, certs, certs.leaf_certificate)
    notification = {"notificationType": "DID_RENEW",
                    "notificationUUID": notification_uuid or str(uuid4()),
                    "data": {"environment": environment,
                             "bundleId": "com.hayden.timefragment",
                             "signedTransactionInfo": transaction_jws, "status": 1},
                    "version": "2.0"}
    return tsc._build_jws(notification, certs, certs.leaf_certificate)


def _run(coroutine):
    return asyncio.run(coroutine)


def test_process_notification_success(certs, configuration):
    backend = WorkerBackend()
    apple = FakeApple(pinned_roots=[certs.root_certificate])
    account_token = str(uuid4())
    signed = _notification_token(certs, app_account_token=account_token)
    result = _run(process_notification(backend=backend, apple_client=apple,
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result["received"] is True and result["bound"] is True
    actions = [call[0] for call in backend.calls]
    assert actions == ["event_receive", "account_by_token", "apple_verify", "event_mark"]
    verify_call = backend.calls[2]
    assert verify_call[0] == "apple_verify"
    assert verify_call[2]["originalTransactionId"] == "123"
    assert verify_call[2]["requireSession"] is False
    assert verify_call[2]["appAccountToken"] == account_token
    assert apple.calls == ["123"]


def test_duplicate_notification_short_circuits(certs, configuration):
    backend = WorkerBackend(event_receive={"received": False})
    signed = _notification_token(certs)
    result = _run(process_notification(backend=backend, apple_client=FakeApple(),
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result == {"received": False, "eventId": result["eventId"]}
    assert [call[0] for call in backend.calls] == ["event_receive"]


def test_unknown_account_token_is_processed_without_binding(certs, configuration):
    backend = WorkerBackend(account_by_token={"code": "ACCOUNT_TOKEN_UNKNOWN"})
    signed = _notification_token(certs)
    result = _run(process_notification(backend=backend, apple_client=FakeApple(),
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result["bound"] is False
    actions = [call[0] for call in backend.calls]
    assert actions == ["event_receive", "account_by_token", "event_mark"]
    assert backend.calls[2][1]["lastErrorCode"] == "ACCOUNT_TOKEN_UNKNOWN"


def test_processing_failure_marks_event_for_retry(certs, configuration):
    backend = WorkerBackend()
    signed = _notification_token(certs)
    with pytest.raises(HTTPException) as error:
        _run(process_notification(backend=backend,
                                  apple_client=FakeApple(error=AppStoreUnavailable("busy")),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 500
    assert error.value.detail["code"] == "EVENT_RETRY_SCHEDULED"
    mark_calls = [call for call in backend.calls if call[0] == "event_mark"]
    assert mark_calls[0][1]["status"] == "failed"


def test_environment_mismatch_rejects_notification(certs, configuration):
    backend = WorkerBackend()
    signed = _notification_token(certs, environment="Production")
    with pytest.raises(HTTPException) as error:
        _run(process_notification(backend=backend, apple_client=FakeApple(),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "ENVIRONMENT_MISMATCH"


def test_reconcile_requeries_every_active_chain(certs, configuration):
    reference = "900002"
    chain = {"userId": str(ACCOUNT_ID), "provider": "apple", "productId": PRODUCT,
             "environment": "sandbox",
             "storeReferenceCiphertext": _encrypted_reference(configuration, reference),
             "purchaseAccountToken": str(uuid4())}
    backend = WorkerBackend(reconcile_chains=[chain])
    apple = FakeApple(payload={"environment": "Sandbox",
        "subscriptionGroupIdentifierItems": [{"subscriptionGroupIdentifier": "group1",
            "subscriptionItems": [{"originalTransactionId": "900002", "status": 1,
                                   "expiresDate": EXPIRES_MS}]}]},
        pinned_roots=[certs.root_certificate])

    async def run():
        try:
            return await reconcile(backend=backend, apple_client=apple,
                                   settings=configuration,
                                   pinned_roots=[certs.root_certificate])
        finally:
            await apple.aclose()

    verified = _run(run())
    assert verified == 1
    verify_calls = [call for call in backend.calls if call[0] == "apple_verify"]
    assert verify_calls[0][2]["originalTransactionId"] == reference
    assert verify_calls[0][2]["requireSession"] is False
    assert verify_calls[0][2]["appAccountToken"] == chain["purchaseAccountToken"]


def test_pending_events_are_retried(certs, configuration):
    pending = {"events": [{"provider": "apple", "environment": "sandbox",
                           "eventId": "evt-1", "attempts": 1,
                           "replayMaterialCiphertext": _notification_token(certs)}]}
    backend = WorkerBackend(event_pending=pending)
    apple = FakeApple(pinned_roots=[certs.root_certificate])

    async def run():
        try:
            return await process_pending_events(backend=backend, apple_client=apple,
                                                settings=configuration,
                                                pinned_roots=[certs.root_certificate])
        finally:
            await apple.aclose()

    processed = _run(run())
    assert processed == 1
    actions = [call[0] for call in backend.calls]
    assert actions == ["event_pending", "event_receive", "account_by_token",
                       "apple_verify", "event_mark"]


def test_webhook_route_success_malformed_and_unconfigured(certs, configuration, monkeypatch):
    signed = _notification_token(certs)

    class PatchedApple(FakeApple):
        def __init__(self, **kwargs):
            super().__init__(pinned_roots=[certs.root_certificate])

    monkeypatch.setattr("app.account_api.AppStoreServerAPIClient", PatchedApple)
    with TestClient(create_account_api(configuration, WorkerBackend())) as client:
        response = client.post("/webhooks/apple", json={"signedPayload": signed})
        assert response.status_code == 200
        assert response.json()["received"] is True

        malformed = client.post("/webhooks/apple", json={"other": 1})
        assert malformed.status_code == 400

    unconfigured = configuration.model_copy(update={
        "apple_private_key": None, "store_reference_key": None})
    with TestClient(create_account_api(unconfigured, WorkerBackend())) as client:
        response = client.post("/webhooks/apple", json={"signedPayload": _b64(b"x")})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "BILLING_NOT_CONFIGURED"
