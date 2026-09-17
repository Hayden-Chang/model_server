import asyncio
import base64
import hashlib
import json
import logging
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
from app.appstore_client import AppStoreUnavailable, _b64url_decode

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
PRODUCT = "com.hayden.daymosaic.plus.monthly"
EXPIRES_MS = 1789000000000
EXPIRES_ISO = datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%S.") + f"{datetime.fromtimestamp(EXPIRES_MS / 1000, timezone.utc).microsecond // 1000:03d}Z"
# ^guest_[a-f0-9]{24}$ : the only actor shape billing_service admits (202609170017).
DEVICE_PRINCIPAL = "guest_0123456789abcdef01234567"


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
        self._account_by_token = account_by_token or {"principal": DEVICE_PRINCIPAL}
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


class LocalEventBackend(WorkerBackend):
    """The billing_service event actions, kept in local state.

    WorkerBackend answers `{"received": True}` to every event_receive, so it
    cannot show the retry net failing: in production the event reached by a
    retry is the caller's own earlier row, and event_receive answers
    `{"received": false}` for it. Every value here mirrors the deployed RPC
    (202609170017): the row is keyed by (provider, environment, event_id), a
    repeat receive answers `received:false` when the payload hash matches and
    `EVENT_CONFLICT` when it does not, event_mark increments `attempts` only
    for `failed`/`processed`, and event_pending selects exactly
    `status in ('received','failed') and attempts < maxAttempts`.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.events = {}

    def seed(self, *, event_id, payload_hash, replay_material, payload_environment,
             status="received", attempts=0):
        self.events[(payload_environment, event_id)] = {
            "provider": "apple", "environment": payload_environment, "eventId": event_id,
            "payloadHash": payload_hash, "replayMaterialCiphertext": replay_material,
            "status": status, "attempts": attempts}

    async def billing_event(self, action, **data):
        self.calls.append((action, data))
        if action == "event_receive":
            key = (data["environment"], data["eventId"])
            stored = self.events.get(key)
            if stored is None:
                self.seed(event_id=data["eventId"], payload_hash=data["payloadHash"],
                          replay_material=data["replayMaterialCiphertext"],
                          payload_environment=data["environment"])
                return {"received": True}
            if stored["payloadHash"] != data["payloadHash"]:
                raise failure("EVENT_CONFLICT", 409)
            return {"received": False}
        if action == "event_mark":
            stored = self.events.get((data["environment"], data["eventId"]))
            stored["status"] = data["status"]
            if data["status"] in ("failed", "processed"):
                stored["attempts"] += 1
            return {"updated": 1}
        if action == "event_pending":
            pending = [dict(row) for row in self.events.values()
                       if row["status"] in ("received", "failed")
                       and row["attempts"] < data["maxAttempts"]]
            return {"events": sorted(pending, key=lambda row: row["eventId"])}
        return await super().billing_event(action, **data)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _encrypted_reference(configuration, reference: str) -> str:
    key = base64.b64decode(configuration.store_reference_key.get_secret_value())
    return encrypt_reference(key, reference)


def _notification_token(certs, *, notification_uuid=None, environment="Sandbox",
                        product=PRODUCT, app_account_token=None,
                        original_transaction_id="123", ownership=None):
    transaction_body = {"originalTransactionId": original_transaction_id,
                        "productId": product,
                        "appAccountToken": app_account_token or str(uuid4()),
                        "environment": environment}
    if ownership is not None:
        # App Store JWS payload field. `ownershipType` is the StoreKit 2
        # client-side Swift property name and never appears in a receipt.
        transaction_body["inAppOwnershipType"] = ownership
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
    # account_by_token now answers with the principal itself, not a user id.
    assert verify_call[1] == Actor(DEVICE_PRINCIPAL, None)
    assert verify_call[2]["originalTransactionId"] == "123"
    # A service-to-service call must never bind the device: binding would clear
    # a customer-support revocation (design §5.3).
    assert verify_call[2]["bindDevice"] is False
    assert "requireSession" not in verify_call[2] and "sessionID" not in verify_call[2]
    assert verify_call[2]["appAccountToken"] == account_token
    assert apple.calls == ["123"]


def test_webhook_stores_the_encrypted_original_transaction_id(certs, configuration):
    # Defect 1: the webhook path stored the raw signed JWS in
    # storeReferenceCiphertext while reconcile() reads that column through
    # decrypt_reference() -- so every chain a webhook touched was undecryptable
    # and reconcile() skipped it forever. The stored value must be the encrypted
    # originalTransactionId, in the same format the purchase path writes
    # (billing_verify.verify_apple_purchase).
    backend = WorkerBackend()
    signed = _notification_token(certs)
    _run(process_notification(backend=backend, apple_client=FakeApple(),
                              settings=configuration, signed_payload=signed,
                              pinned_roots=[certs.root_certificate]))
    verify_call = [call for call in backend.calls if call[0] == "apple_verify"][0]
    stored = verify_call[2]["storeReferenceCiphertext"]
    assert stored != signed
    key = base64.b64decode(configuration.store_reference_key.get_secret_value())
    # "123" is the originalTransactionId the fixture notification carries.
    assert decrypt_reference(key, stored) == "123"


def test_chain_written_by_the_webhook_reconciles(certs, configuration):
    # Defect 1 round trip: the ciphertext the webhook hands to the RPC is the
    # ciphertext a later reconcile() pass decrypts. Before the fix this chain was
    # dropped with an InvalidTag warning, so verified stayed 0.
    webhook_backend = WorkerBackend()
    _run(process_notification(backend=webhook_backend, apple_client=FakeApple(),
                              settings=configuration,
                              signed_payload=_notification_token(certs),
                              pinned_roots=[certs.root_certificate]))
    stored = [call for call in webhook_backend.calls
              if call[0] == "apple_verify"][0][2]["storeReferenceCiphertext"]

    chain = {"principal": DEVICE_PRINCIPAL, "provider": "apple", "productId": PRODUCT,
             "environment": "sandbox", "storeReferenceCiphertext": stored,
             "purchaseAccountToken": str(uuid4())}
    backend = WorkerBackend(reconcile_chains=[chain])
    apple = FakeApple(pinned_roots=[certs.root_certificate])
    verified = _run(reconcile(backend=backend, apple_client=apple,
                              settings=configuration,
                              pinned_roots=[certs.root_certificate]))
    assert verified == 1
    assert apple.calls == ["123"]


def test_family_shared_notification_is_rejected_before_any_write(certs, configuration):
    # Design §5.5: a family-shared transaction must not enter through the
    # webhook path either. The gate sits before event_receive, so nothing at all
    # is written for this notification.
    backend = WorkerBackend()
    signed = _notification_token(certs, ownership="FAMILY_SHARED")
    with pytest.raises(HTTPException) as error:
        _run(process_notification(backend=backend, apple_client=FakeApple(),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "FAMILY_SHARING_NOT_ALLOWED"
    assert backend.calls == []


def test_purchased_notification_passes_the_family_sharing_gate(certs, configuration):
    backend = WorkerBackend()
    signed = _notification_token(certs, ownership="PURCHASED")
    result = _run(process_notification(backend=backend, apple_client=FakeApple(),
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result["bound"] is True
    assert [call[0] for call in backend.calls] == ["event_receive", "account_by_token",
                                                   "apple_verify", "event_mark"]


def test_absent_ownership_type_passes_the_notification_gate(certs, configuration, caplog):
    # Fail-open direction, pinned deliberately (design §5.5 / M0.5): an absent
    # field must not reject a genuine transaction, but it must be logged.
    backend = WorkerBackend()
    signed = _notification_token(certs)
    with caplog.at_level(logging.WARNING, logger="app.billing_verify"):
        result = _run(process_notification(backend=backend, apple_client=FakeApple(),
                                           settings=configuration, signed_payload=signed,
                                           pinned_roots=[certs.root_certificate]))
    assert result["bound"] is True
    assert "inAppOwnershipType" in caplog.text


def test_family_shared_notification_is_not_bound_through_the_webhook(certs, configuration, monkeypatch):
    # Consequence of placing the gate before event_receive (design §5.5): no
    # billing_events row is written, so nothing deduplicates this notification and
    # Apple is told to retry -- for a v2 notification that is five more attempts
    # at 1/12/24/48/72h (production only). A terminal ack is not available here:
    # without a row there is nothing to event_mark, so terminating it would mean
    # answering 2xx and dropping the notification with no record at all. Recorded
    # as the open product question in the P1 report rather than decided here.
    class PatchedApple(FakeApple):
        def __init__(self, **kwargs):
            super().__init__(pinned_roots=[certs.root_certificate])

    monkeypatch.setattr("app.account_api.AppStoreServerAPIClient", PatchedApple)
    backend = WorkerBackend()
    signed = _notification_token(certs, ownership="FAMILY_SHARED")
    with TestClient(create_account_api(configuration, backend),
                    raise_server_exceptions=False) as client:
        response = client.post("/webhooks/apple", json={"signedPayload": signed})
    assert backend.calls == []
    assert response.status_code == 500
    # A structured retry signal, not the bare 500 the mapping defect produced.
    assert response.json()["detail"]["code"] == "EVENT_RETRY_SCHEDULED"
    assert response.json()["detail"]["upstreamCode"] == "FAMILY_SHARING_NOT_ALLOWED"


def test_duplicate_notification_short_circuits(certs, configuration):
    backend = WorkerBackend(event_receive={"received": False})
    signed = _notification_token(certs)
    result = _run(process_notification(backend=backend, apple_client=FakeApple(),
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result == {"received": False, "eventId": result["eventId"]}
    assert [call[0] for call in backend.calls] == ["event_receive"]


def test_unknown_account_token_raises_instead_of_being_skipped(certs, configuration):
    # Corrected by M4 (design §0.3 C6). This test used to assert the
    # "clean skip" branch in process_notification, which production cannot
    # reach: AccountBackend.billing_event() raises on every RPC `code` (pinned
    # by test_billing_api.test_billing_event_raises_on_every_rpc_code), so the
    # code-carrying dict never arrives. The fake therefore raises, like the real
    # backend, and the assertion is on the behaviour that actually happens.
    #
    # The event row does exist here (event_receive already wrote it), so the
    # failure is recorded like every other post-receive failure instead of
    # escaping with last_error_code null. Retry, not the terminal ack of the
    # branch below, is deliberate -- the chain is still unbound, so marking the
    # event "processed" would retire its only pending marker. See the P1
    # report's open question.
    backend = WorkerBackend(account_by_token=failure("ACCOUNT_TOKEN_UNKNOWN", 404))
    signed = _notification_token(certs)
    with pytest.raises(HTTPException) as error:
        _run(process_notification(backend=backend, apple_client=FakeApple(),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 404
    assert error.value.detail["code"] == "ACCOUNT_TOKEN_UNKNOWN"
    assert [call[0] for call in backend.calls] == ["event_receive", "account_by_token",
                                                   "event_mark"]
    assert backend.calls[2][1]["status"] == "failed"
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


def test_webhook_maps_an_apple_outage_to_a_structured_retry(certs, configuration):
    # Defect 2: handle_apple_webhook forwarded the upstream detail dict -- which
    # failure() always fills with its own "code" -- into failure(code=...), so
    # every processing failure raised TypeError: Apple saw a bare 500 with no
    # structured code and log_http_failure() never ran.
    backend = WorkerBackend()
    signed = _notification_token(certs)
    with pytest.raises(HTTPException) as error:
        _run(handle_apple_webhook(backend=backend,
                                  apple_client=FakeApple(error=AppStoreUnavailable("busy")),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 500
    assert error.value.detail["code"] == "EVENT_RETRY_SCHEDULED"
    # The retry signal must not swallow why we are retrying.
    assert error.value.detail["upstreamCode"] == "AppStoreUnavailable"
    assert error.value.detail["eventId"]
    mark_calls = [call for call in backend.calls if call[0] == "event_mark"]
    assert mark_calls[0][1]["status"] == "failed"
    assert mark_calls[0][1]["lastErrorCode"] == "AppStoreUnavailable"


def test_webhook_maps_an_unknown_account_token_to_a_structured_retry(certs, configuration):
    # Second, distinct cause: the failure is raised by account_by_token after the
    # row exists, not by the Apple client.
    backend = WorkerBackend(account_by_token=failure("ACCOUNT_TOKEN_UNKNOWN", 404))
    signed = _notification_token(certs)
    with pytest.raises(HTTPException) as error:
        _run(handle_apple_webhook(backend=backend, apple_client=FakeApple(),
                                  settings=configuration, signed_payload=signed,
                                  pinned_roots=[certs.root_certificate]))
    assert error.value.status_code == 500
    assert error.value.detail["code"] == "EVENT_RETRY_SCHEDULED"
    assert error.value.detail["upstreamCode"] == "ACCOUNT_TOKEN_UNKNOWN"


def test_webhook_retry_failure_is_logged_and_returns_a_structured_500(certs, configuration,
                                                                    monkeypatch, caplog):
    class PatchedApple(FakeApple):
        def __init__(self, **kwargs):
            super().__init__(pinned_roots=[certs.root_certificate],
                             error=AppStoreUnavailable("busy"))

    monkeypatch.setattr("app.account_api.AppStoreServerAPIClient", PatchedApple)
    with caplog.at_level(logging.WARNING, logger="app.account_api"):
        with TestClient(create_account_api(configuration, WorkerBackend()),
                        raise_server_exceptions=False) as client:
            response = client.post("/webhooks/apple",
                                   json={"signedPayload": _notification_token(certs)})
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "EVENT_RETRY_SCHEDULED"
    # log_http_failure() records a code only when it is in SAFE_ERROR_CODES, so a
    # retry code missing from that set is silently logged as HTTP_ERROR.
    assert "EVENT_RETRY_SCHEDULED" in caplog.text


def test_webhook_re_raises_client_errors_unchanged(certs, configuration):
    # 400/409 are client/contract errors: they must not be turned into the retry
    # signal, or into a 500.
    with pytest.raises(HTTPException) as mismatch:
        _run(handle_apple_webhook(backend=WorkerBackend(), apple_client=FakeApple(),
                                  settings=configuration,
                                  signed_payload=_notification_token(certs, environment="Production"),
                                  pinned_roots=[certs.root_certificate]))
    assert mismatch.value.status_code == 400
    assert mismatch.value.detail["code"] == "ENVIRONMENT_MISMATCH"

    conflicting = WorkerBackend(event_receive=failure("EVENT_CONFLICT", 409))
    with pytest.raises(HTTPException) as conflict:
        _run(handle_apple_webhook(backend=conflicting, apple_client=FakeApple(),
                                  settings=configuration,
                                  signed_payload=_notification_token(certs),
                                  pinned_roots=[certs.root_certificate]))
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "EVENT_CONFLICT"


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
    chain = {"principal": DEVICE_PRINCIPAL, "provider": "apple", "productId": PRODUCT,
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
    assert verify_calls[0][1] == Actor(DEVICE_PRINCIPAL, None)
    assert verify_calls[0][2]["originalTransactionId"] == reference
    # reconcile_list now returns `principal`; reconcile must not bind (design §5.3).
    assert verify_calls[0][2]["bindDevice"] is False
    assert "requireSession" not in verify_calls[0][2]
    assert verify_calls[0][2]["appAccountToken"] == chain["purchaseAccountToken"]


def test_reconcile_reports_a_corrupt_reference_without_aborting_the_sweep(certs, configuration,
                                                                        caplog):
    # The self-concealing half of defect 1: an undecryptable
    # storeReferenceCiphertext was swallowed as an ordinary warning,
    # indistinguishable from a routine skip. A JWS in that column is exactly what
    # the webhook path used to write.
    corrupt = {"principal": DEVICE_PRINCIPAL, "provider": "apple", "productId": PRODUCT,
               "environment": "sandbox",
               "storeReferenceCiphertext": _notification_token(certs),
               "purchaseAccountToken": str(uuid4())}
    healthy = {"principal": "guest_89abcdef0123456789abcdef", "provider": "apple",
               "productId": PRODUCT, "environment": "sandbox",
               "storeReferenceCiphertext": _encrypted_reference(configuration, "123"),
               "purchaseAccountToken": str(uuid4())}
    backend = WorkerBackend(reconcile_chains=[corrupt, healthy])
    with caplog.at_level(logging.DEBUG, logger="app.billing_worker"):
        verified = _run(reconcile(backend=backend, apple_client=FakeApple(),
                                  settings=configuration,
                                  pinned_roots=[certs.root_certificate]))
    # One corrupt chain must not stop the rest of the sweep ...
    assert verified == 1
    # ... but it must be visible, and name the chain it belongs to.
    records = [record for record in caplog.records if record.name == "app.billing_worker"]
    assert [record.levelno for record in records] == [logging.ERROR]
    assert DEVICE_PRINCIPAL in records[0].getMessage()


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
    # event_receive is deliberately absent: the stored replay material is our own
    # earlier row being replayed, not a second delivery of it.
    assert actions == ["event_pending", "account_by_token", "apple_verify", "event_mark"]
    assert apple.calls == ["123"]


def test_retry_after_a_recorded_receipt_reaches_apple_and_marks_the_event(certs, configuration):
    # Defect 3: process_pending_events re-fed the stored replay material into
    # process_notification, which called event_receive first. For a retry the
    # event row is the caller's own earlier row, so event_receive answered
    # `received:false` and process_notification returned before account_by_token,
    # before the Apple call and before any event_mark. The retry pass counted the
    # event as processed while doing nothing at all. The retry is our own replay
    # of a recorded event, not another delivery of it, so it must skip the
    # receive and run the processing path.
    signed = _notification_token(certs)
    event_id = str(json.loads(_b64url_decode(signed.split(".")[1]))["notificationUUID"])
    # Delivered before this pass: stored, but nothing processed it. 'failed' is
    # what a processing failure leaves behind, 'received' what a crash leaves.
    for status in ("received", "failed"):
        backend = LocalEventBackend()
        backend.seed(event_id=event_id,
                     payload_hash=hashlib.sha256(signed.encode()).hexdigest(),
                     replay_material=signed, payload_environment="sandbox",
                     status=status, attempts=1)
        apple = FakeApple(pinned_roots=[certs.root_certificate])

        async def run():
            try:
                return await process_pending_events(backend=backend, apple_client=apple,
                                                    settings=configuration,
                                                    pinned_roots=[certs.root_certificate])
            finally:
                await apple.aclose()

        assert _run(run()) == 1
        actions = [call[0] for call in backend.calls]
        assert "apple_verify" in actions, f"retry of a {status} event never reached Apple"
        marks = [call for call in backend.calls if call[0] == "event_mark"]
        assert [call[1]["status"] for call in marks] == ["processed"]
        assert apple.calls == ["123"]
        row = backend.events[("sandbox", event_id)]
        assert row["status"] == "processed"
        # The attempt was counted: this is what maxAttempts bounds.
        assert row["attempts"] == 2
        # A processed event is no longer pending, so the pass cannot re-select it.
        assert _run(run()) == 0


def test_retry_attempts_advance_until_the_event_ages_out(certs, configuration):
    # Structural half of the defect: attempts could never increase, because the
    # early return happened before event_mark. event_pending selects
    # `status in ('received','failed') and attempts < maxAttempts`, so the row
    # was selected on every pass forever and maxAttempts bounded nothing.
    signed = _notification_token(certs)
    event_id = str(json.loads(_b64url_decode(signed.split(".")[1]))["notificationUUID"])
    backend = LocalEventBackend()
    backend.seed(event_id=event_id,
                 payload_hash=hashlib.sha256(signed.encode()).hexdigest(),
                 replay_material=signed, payload_environment="sandbox",
                 status="failed", attempts=0)
    apple = FakeApple(error=AppStoreUnavailable("busy"),
                      pinned_roots=[certs.root_certificate])

    async def run():
        try:
            return await process_pending_events(backend=backend, apple_client=apple,
                                                settings=configuration,
                                                pinned_roots=[certs.root_certificate],
                                                max_attempts=3)
        finally:
            await apple.aclose()

    for expected_attempts in (1, 2, 3):
        # 0 successful events: the pass reports no progress when the upstream
        # call fails, and the failed mark is what advances the counter.
        assert _run(run()) == 0
        assert backend.events[("sandbox", event_id)]["attempts"] == expected_attempts
    # Bound reached: event_pending no longer selects the row.
    assert _run(run()) == 0
    assert backend.events[("sandbox", event_id)]["attempts"] == 3
    # A failing retry marks the event failed, so the same counter bounds the
    # worker retry and it never has to fake a success to make progress.
    marks = [call for call in backend.calls
             if call[0] == "event_mark" and call[1]["status"] == "failed"]
    assert len(marks) == 3
    assert all(call[1]["lastErrorCode"] == "AppStoreUnavailable" for call in marks)
    assert apple.calls == ["123", "123", "123"]


def test_fresh_delivery_of_a_duplicate_event_is_still_deduplicated(certs, configuration):
    # The receive step -- and with it the dedupe contract (same eventId, same
    # payload hash -> already received) -- still runs for a fresh delivery, and a
    # duplicate must not be processed, bound or marked a second time.
    signed = _notification_token(certs)
    event_id = str(json.loads(_b64url_decode(signed.split(".")[1]))["notificationUUID"])
    backend = LocalEventBackend()
    backend.seed(event_id=event_id,
                 payload_hash=hashlib.sha256(signed.encode()).hexdigest(),
                 replay_material=signed, payload_environment="sandbox",
                 status="processed", attempts=1)
    apple = FakeApple(pinned_roots=[certs.root_certificate])
    result = _run(process_notification(backend=backend, apple_client=apple,
                                       settings=configuration, signed_payload=signed,
                                       pinned_roots=[certs.root_certificate]))
    assert result == {"received": False, "eventId": event_id}
    assert [call[0] for call in backend.calls] == ["event_receive"]
    assert apple.calls == []
    row = backend.events[("sandbox", event_id)]
    assert row["status"] == "processed" and row["attempts"] == 1


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
