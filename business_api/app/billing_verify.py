"""Apple purchase verification orchestration (Phase A4).

The route never trusts client claims: it verifies the signed transaction
against the pinned Apple root, re-queries Apple for the authoritative
subscription state, and only then delegates the unique binding and
entitlement aggregation to the billing_service RPC.
"""

import base64
import logging
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

LOGGER = logging.getLogger(__name__)
STATUS_MAP = {1: "active", 2: "expired", 3: "billing_retry", 4: "revoked"}

# App Store JWS payload field. `ownershipType` is the StoreKit 2 client-side
# Swift property name; reading that name here would always yield None.
OWNERSHIP_TYPE_FIELD = "inAppOwnershipType"
FAMILY_SHARED = "FAMILY_SHARED"


def reject_family_shared(transaction: dict) -> None:
    """Refuse a transaction shared through Family Sharing (design §5.5).

    Only the exact value `FAMILY_SHARED` is rejected, and an absent field is
    allowed through. That is a deliberate fail-open direction: requiring
    `PURCHASED` would reject genuine purchases whenever Apple omits the field,
    and M0.5 has not yet confirmed that `inAppOwnershipType` is present on live
    non-shared receipts. The absent case is logged so a missing field shows up
    in production instead of silently disabling this gate. Re-confirm on real
    sandbox/production receipts at the M0.5 pre-launch check; if the field
    turns out not to be always present, tighten this to require `PURCHASED`.
    """
    ownership = transaction.get(OWNERSHIP_TYPE_FIELD)
    if ownership is None:
        LOGGER.warning("apple %s field absent; family-sharing gate not applied",
                       OWNERSHIP_TYPE_FIELD)
        return
    if str(ownership) == FAMILY_SHARED:
        raise failure("FAMILY_SHARING_NOT_ALLOWED", 422)


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
                                pinned_roots=None, synchronize_current=False) -> dict:
    try:
        transaction = verify_apple_jws(payload_body.signed_transaction, pinned_roots=pinned_roots)
    except JWSVerificationFailed as error:
        raise failure("VERIFICATION_FAILED", 422, reason=str(error)) from error
    reject_family_shared(transaction)
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
        # AppStoreUnavailable covers four very different faults -- transport
        # failure, 5xx/429 from Apple, an unparsable envelope, and (before this
        # was fixed) a hostname that did not exist. Collapsing them into a bare
        # 202 made a wrong constant indistinguishable from a transient outage and
        # cost a long hunt. Keep the client-facing code unchanged (the app treats
        # 202 as "retry later") but carry the reason, exactly as the 422 branch
        # below already does.
        LOGGER.error("app store status unavailable for chain %s: %s",
                     original_transaction_id, error)
        raise failure("VERIFICATION_PENDING", 202, reason=str(error)) from error
    except (AppStoreRejected, AppStoreEnvironmentMismatch, JWSVerificationFailed) as error:
        raise failure("VERIFICATION_FAILED", 422, reason=str(error)) from error
    store_status, expires_at = subscription_state(status_payload, original_transaction_id)
    # What the client actually submitted vs what Apple reports for the chain.
    # When these disagree the client is handing back an old transaction from
    # StoreKit's queue instead of a fresh purchase, and the whole flow looks
    # like a server fault from the outside. Diagnosing that previously required
    # decoding the receipt by hand, so log the comparison.
    LOGGER.info(
        "apple verify chain=%s submitted tx=%s purchaseDate=%s expiresDate=%s | "
        "apple status=%s expiresAt=%s",
        original_transaction_id,
        transaction.get("transactionId"),
        _iso_millis(transaction.get("purchaseDate")),
        _iso_millis(transaction.get("expiresDate")),
        store_status, expires_at)
    data = dict(originalTransactionId=original_transaction_id, productId=product_id,
                appAccountToken=app_account_token, environment=settings.apple_environment,
                storeStatus=store_status, expiresAt=expires_at,
                storeReferenceCiphertext=encrypt_reference(
                    _reference_key_bytes(settings), original_transaction_id),
                # Client purchase/restore path: this call may join the requesting
                # device to the chain (design §5.3).
                bindDevice=True,
                claimId=str(payload_body.claim_id) if payload_body.claim_id else None)
    return await backend.billing("apple_sync" if synchronize_current else "apple_verify", actor, **data)
