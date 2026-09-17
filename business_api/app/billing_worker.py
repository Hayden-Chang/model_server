"""Apple Server Notifications V2 processing and reconciliation (Phase A5).

The webhook endpoint only verifies and deduplicates: processing failures stay
in `billing_events` with their encrypted replay material so the billing worker
can retry them idempotently, and Apple's own retries act as a second net.
"""

import asyncio
import base64
import hashlib
import logging
import os

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException

from .account_backend import AccountBackend, Actor, failure, resolve_apple_key_p8
from .billing_verify import (decrypt_reference, encrypt_reference, reject_family_shared,
                             subscription_state)
from .appstore_client import (
    AppStoreRejected,
    AppStoreUnavailable,
    JWSVerificationFailed,
    verify_apple_jws,
)

LOGGER = logging.getLogger(__name__)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _reference_key_bytes(settings) -> bytes:
    value = settings.store_reference_key
    if value is None:
        raise failure("BILLING_NOT_CONFIGURED", 503)
    return base64.b64decode(value.get_secret_value())


async def process_notification(*, backend, apple_client, settings, signed_payload: str,
                               pinned_roots=None) -> dict:
    """Verify, deduplicate and process one Apple notification."""

    try:
        notification = verify_apple_jws(signed_payload, pinned_roots=pinned_roots)
    except JWSVerificationFailed as error:
        raise failure("VERIFICATION_FAILED", 400, reason=str(error)) from error
    event_id = str(notification.get("notificationUUID") or "")
    if not event_id:
        raise failure("VERIFICATION_FAILED", 400, reason="MISSING_NOTIFICATION_UUID")
    data = notification.get("data") or {}
    payload_environment = str(data.get("environment", "")).lower()
    if payload_environment and payload_environment != settings.apple_environment:
        raise failure("ENVIRONMENT_MISMATCH", 400)
    transaction = verify_apple_jws(str(data.get("signedTransactionInfo") or ""),
                                   pinned_roots=pinned_roots)
    # A family-shared transaction must not enter through the webhook path either
    # (design §5.5): the same signed payload is rejected as on the client path.
    reject_family_shared(transaction)
    original_transaction_id = str(transaction.get("originalTransactionId") or "")
    if not original_transaction_id:
        raise failure("VERIFICATION_FAILED", 400, reason="MISSING_ORIGINAL_TRANSACTION_ID")
    payload_hash = _sha256_hex(signed_payload)

    received = await backend.billing_event("event_receive", provider="apple",
        environment=settings.apple_environment, eventId=event_id,
        payloadHash=payload_hash, replayMaterialCiphertext=signed_payload)
    if not received.get("received", False):
        return {"received": False, "eventId": event_id}

    try:
        account = await backend.billing_event("account_by_token",
            appAccountToken=str(transaction.get("appAccountToken") or ""))
    except HTTPException as error:
        # billing_event() raises on every RPC code (pinned by
        # test_billing_api.test_billing_event_raises_on_every_rpc_code), so an
        # unknown token arrives here and never as the dict handled below. The
        # event row already exists, so this failure is recorded like every other
        # post-receive one -- unmarked, last_error_code stays null and the event
        # is undiagnosable in billing_events. Retry, not the terminal ack the
        # branch below intends, is deliberate: the chain is still unbound, so
        # claiming "processed" would retire the only pending marker for it. The
        # alternative is an open product decision (see the P1 report).
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        if code != "ACCOUNT_TOKEN_UNKNOWN":
            raise
        await backend.billing_event("event_mark", provider="apple",
            environment=settings.apple_environment, eventId=event_id,
            status="failed", lastErrorCode="ACCOUNT_TOKEN_UNKNOWN")
        raise
    # Unreachable while AccountBackend.billing_event() raises on every RPC code
    # (pinned by test_billing_api.test_billing_event_raises_on_every_rpc_code);
    # kept as the intended handling if that mapping ever stops raising.
    if account.get("code") == "ACCOUNT_TOKEN_UNKNOWN":
        await backend.billing_event("event_mark", provider="apple",
            environment=settings.apple_environment, eventId=event_id,
            status="processed", lastErrorCode="ACCOUNT_TOKEN_UNKNOWN")
        return {"received": True, "bound": False, "eventId": event_id}
    actor_principal = str(account["principal"])

    try:
        status_payload = await apple_client.subscription_status(original_transaction_id)
        store_status, expires_at = subscription_state(status_payload,
                                                      original_transaction_id)
        # bindDevice=False: this call acts as the chain's principal, not as the
        # requesting device. Letting it bind would clear a customer-support
        # revocation (design §5.3).
        await backend.billing("apple_verify",
            Actor(actor_principal, None), bindDevice=False,
            originalTransactionId=original_transaction_id,
            productId=str(transaction.get("productId") or ""),
            appAccountToken=str(transaction.get("appAccountToken") or ""),
            environment=settings.apple_environment, storeStatus=store_status,
            expiresAt=expires_at,
            # Same ciphertext format as the purchase path: reconcile() reads this
            # column through decrypt_reference(), so storing the raw JWS here made
            # every chain a webhook touched undecryptable.
            storeReferenceCiphertext=encrypt_reference(
                _reference_key_bytes(settings), original_transaction_id),
            claimId=None)
    except (HTTPException, AppStoreUnavailable, AppStoreRejected) as error:
        detail_code = (error.detail.get("code", "PROCESSING_FAILED")
                       if isinstance(getattr(error, "detail", None), dict)
                       else type(error).__name__)
        await backend.billing_event("event_mark", provider="apple",
            environment=settings.apple_environment, eventId=event_id,
            status="failed", lastErrorCode=detail_code)
        raise failure("EVENT_RETRY_SCHEDULED", 500, eventId=event_id,
                      upstreamCode=detail_code) from error

    await backend.billing_event("event_mark", provider="apple",
        environment=settings.apple_environment, eventId=event_id, status="processed")
    return {"received": True, "bound": True, "eventId": event_id,
            "notificationType": str(notification.get("notificationType") or "")}


async def handle_apple_webhook(*, backend, apple_client, settings, signed_payload: str,
                               pinned_roots=None) -> dict:
    """Webhook entry: never fail on duplicates, always retry on processing errors."""

    if apple_client is None or settings.store_reference_key is None:
        raise failure("BILLING_NOT_CONFIGURED", 503)
    try:
        return await process_notification(backend=backend, apple_client=apple_client,
                                          settings=settings, signed_payload=signed_payload,
                                          pinned_roots=pinned_roots)
    except HTTPException as error:
        if error.status_code in (400, 409):
            raise
        # failure() always stores its own "code" -- and takes "status" -- in the
        # detail dict, so forwarding that dict verbatim passed "code" twice and
        # raised TypeError: every processing failure became a bare 500 with no
        # structured code and no log_http_failure() record. EVENT_RETRY_SCHEDULED
        # stays the authoritative code (the retry signal, and what
        # log_http_failure records); the upstream cause travels beside it so the
        # failure stays diagnosable.
        upstream = error.detail if isinstance(error.detail, dict) else {}
        forwarded = {key: value for key, value in upstream.items()
                     if key not in ("code", "status", "upstreamCode")}
        raise failure("EVENT_RETRY_SCHEDULED", 500,
                      upstreamCode=upstream.get("upstreamCode") or upstream.get("code")
                                   or type(error).__name__,
                      **forwarded) from error


async def process_pending_events(*, backend, apple_client, settings, pinned_roots=None,
                                 max_attempts: int = 8, batch: int = 20) -> int:
    fetch = await backend.billing_event("event_pending", maxAttempts=max_attempts,
                                        limit=batch)
    processed = 0
    for event in fetch.get("events", []):
        try:
            await process_notification(backend=backend, apple_client=apple_client,
                                       settings=settings,
                                       signed_payload=event["replayMaterialCiphertext"],
                                       pinned_roots=pinned_roots)
            processed += 1
        except HTTPException as error:
            LOGGER.warning("event retry failed: %s %s", event["eventId"],
                           error.detail.get("code"))
    return processed


async def reconcile(*, backend, apple_client, settings, pinned_roots=None) -> int:
    chains = (await backend.billing_event("reconcile_list")).get("chains", [])
    verified = 0
    for chain in chains:
        try:
            reference = decrypt_reference(
                base64.b64decode(settings.store_reference_key.get_secret_value()),
                chain["storeReferenceCiphertext"])
        except (InvalidTag, KeyError, ValueError) as error:
            # Corruption, not a routine skip: an undecryptable reference at ERROR
            # level, naming the chain it belongs to, instead of hiding among the
            # ordinary warnings. Skipping the chain is still the behaviour --
            # aborting the sweep would let one corrupt row block every other one.
            LOGGER.error("reconcile reference decrypt failed: principal=%s %s",
                         chain.get("principal"), error)
            continue
        try:
            status_payload = await apple_client.subscription_status(reference)
            store_status, expires_at = subscription_state(status_payload, reference)
            # bindDevice=False for the same reason as process_notification: a
            # reconcile pass must not revive a revoked device (design §5.3).
            await backend.billing("apple_verify",
                Actor(str(chain["principal"]), None), bindDevice=False,
                originalTransactionId=reference, productId=chain["productId"],
                appAccountToken=chain.get("purchaseAccountToken") or "",
                environment=chain["environment"], storeStatus=store_status,
                expiresAt=expires_at,
                storeReferenceCiphertext=chain["storeReferenceCiphertext"],
                claimId=None)
            verified += 1
        except HTTPException as error:
            LOGGER.warning("reconcile chain failed: %s", error.detail.get("code"))
    return verified


async def main_async() -> None:
    from .account_backend import AccountAPISettings
    from .appstore_client import AppStoreServerAPIClient

    logging.basicConfig(level=logging.INFO)
    settings = AccountAPISettings()
    backend = AccountBackend(settings)
    interval = int(os.environ.get("BILLING_WORKER_INTERVAL_SECONDS", "300"))
    apple_client = None
    apple_key_p8 = resolve_apple_key_p8(settings)
    if apple_key_p8 is not None:
        apple_client = AppStoreServerAPIClient(
            environment=settings.apple_environment, key_p8=apple_key_p8,
            key_id=settings.apple_key_id, issuer_id=settings.apple_issuer_id,
            bundle_id=settings.apple_bundle_id)
    while True:
        try:
            processed = await process_pending_events(backend=backend,
                apple_client=apple_client, settings=settings)
            if apple_client is not None:
                await reconcile(backend=backend, apple_client=apple_client,
                                settings=settings)
            LOGGER.info("billing worker tick: %s events processed", processed)
        except Exception:
            LOGGER.exception("billing worker tick failed")
        await asyncio.sleep(interval)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
