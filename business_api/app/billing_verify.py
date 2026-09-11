"""Apple purchase verification orchestration (Phase A4).

The route never trusts client claims: it verifies the signed transaction
against the pinned Apple root, re-queries Apple for the authoritative
subscription state, and only then delegates the unique binding and
entitlement aggregation to the billing_service RPC.
"""

import base64
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .account_backend import failure
from .appstore_client import (
    AppStoreEnvironmentMismatch,
    AppStoreRejected,
    AppStoreUnavailable,
    JWSVerificationFailed,
    verify_apple_jws,
)

STATUS_MAP = {1: "active", 2: "expired", 3: "billing_retry", 4: "revoked"}


def _iso_millis(milliseconds) -> str | None:
    if milliseconds is None:
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def encrypt_reference(key: bytes, plaintext: str) -> str:
    nonce = os.urandom(12)
    return base64.urlsafe_b64encode(
        nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), None)).decode()


def decrypt_reference(key: bytes, blob: str) -> str:
    raw = base64.urlsafe_b64decode(blob.encode())
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()


def _reference_key_bytes(settings) -> bytes:
    value = settings.store_reference_key
    if value is None:
        raise failure("BILLING_NOT_CONFIGURED", 503)
    return base64.b64decode(value.get_secret_value())


def subscription_state(status_payload: dict, original_transaction_id: str) -> tuple[str, str | None]:
    """Extract the authoritative (store_status, expiresAt) for one purchase chain."""

    for group in status_payload.get("subscriptionGroupIdentifierItems") or []:
        for item in group.get("subscriptionItems") or []:
            if str(item.get("originalTransactionId")) == original_transaction_id:
                return STATUS_MAP.get(int(item.get("status")), "expired"), _iso_millis(item.get("expiresDate"))
    raise failure("VERIFICATION_FAILED", 422, reason="TRANSACTION_NOT_FOUND")


async def verify_apple_purchase(*, backend, apple_client, settings, actor, payload_body,
                                pinned_roots=None) -> dict:
    try:
        transaction = verify_apple_jws(payload_body.signed_transaction, pinned_roots=pinned_roots)
    except JWSVerificationFailed as error:
        raise failure("VERIFICATION_FAILED", 422, reason=str(error)) from error
    if str(transaction.get("environment", "")).lower() != settings.apple_environment:
        raise failure("ENVIRONMENT_MISMATCH", 422)
    original_transaction_id = str(transaction.get("originalTransactionId") or "")
    if not original_transaction_id:
        raise failure("VERIFICATION_FAILED", 422, reason="MISSING_ORIGINAL_TRANSACTION_ID")
    product_id = payload_body.product_id
    if transaction.get("productId") != product_id:
        raise failure("PRODUCT_MISMATCH", 422)
    if product_id not in [value.strip() for value in settings.apple_product_ids.split(",")]:
        raise failure("PRODUCT_INVALID", 422)
    app_account_token = str(transaction.get("appAccountToken") or "")
    try:
        status_payload = await apple_client.subscription_status(original_transaction_id)
    except AppStoreUnavailable as error:
        raise failure("VERIFICATION_PENDING", 202) from error
    except (AppStoreRejected, AppStoreEnvironmentMismatch, JWSVerificationFailed) as error:
        raise failure("VERIFICATION_FAILED", 422, reason=str(error)) from error
    store_status, expires_at = subscription_state(status_payload, original_transaction_id)
    data = dict(originalTransactionId=original_transaction_id, productId=product_id,
                appAccountToken=app_account_token, environment=settings.apple_environment,
                storeStatus=store_status, expiresAt=expires_at,
                storeReferenceCiphertext=encrypt_reference(
                    _reference_key_bytes(settings), original_transaction_id),
                claimId=str(payload_body.claim_id) if payload_body.claim_id else None)
    return await backend.billing("apple_verify", actor, **data)
