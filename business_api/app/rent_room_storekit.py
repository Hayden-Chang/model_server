"""Native consumable fulfillment, separate from WeChat signing and orders."""
import asyncio
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from .appstore_client import build_client_token


class StoreKitFailure(Exception):
    def __init__(self, message="购买记录暂时无法确认，请稍后重试", status=503):
        self.message, self.status = message, status


class TransactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transactionId: str = Field(pattern=r"^[0-9]{1,32}$")


class NotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signedPayload: str = Field(min_length=1, max_length=60000)


class AppleStoreKit:
    def __init__(self, settings, *, http=None, verifier=None):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=10)
        self.verifier = verifier
        self.credential = None
        if settings.storekit_configured:
            from appstoreserverlibrary.models.Environment import Environment
            from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding
            roots = x509.load_pem_x509_certificates(Path(__file__).with_name("apple_root_ca_g3.pem").read_bytes())
            self.verifier = verifier or SignedDataVerifier([c.public_bytes(Encoding.DER) for c in roots], True,
                Environment(settings.storekit_environment), settings.ios_bundle_id, settings.storekit_app_apple_id)
            # Credentials are read only from a mounted service-side file.
            self.key = Path(settings.storekit_key_path).read_bytes()

    async def close(self):
        await self.http.aclose()

    def _authorize(self):
        if not self.credential or time.monotonic() - self.credential[1] > 240:
            token = build_client_token(key_p8=self.key, key_id=self.settings.storekit_key_id,
                issuer_id=self.settings.storekit_issuer_id, bundle_id=self.settings.ios_bundle_id)
            self.credential = token, time.monotonic()
        return self.credential[0]

    def validate(self, value):
        """Additional product/account constraints on the official SDK result."""
        data = {name: getattr(value, name, None) for name in ["transactionId", "bundleId", "productId", "appAccountToken",
                "environment", "type", "inAppOwnershipType", "quantity", "signedDate", "purchaseDate", "revocationDate"]}
        for name in ["environment", "type", "inAppOwnershipType"]:
            data[name] = getattr(data[name], "value", data[name])
        try:
            data["appAccountToken"] = str(uuid.UUID(str(data["appAccountToken"])))
            valid = [re.fullmatch(r"[0-9]{1,32}", str(data["transactionId"])),
                     data["bundleId"] == self.settings.ios_bundle_id,
                     data["environment"] == self.settings.storekit_environment,
                     data["productId"] == self.settings.storekit_product_id,
                     data["type"] == "Consumable", data["inAppOwnershipType"] == "PURCHASED",
                     type(data["quantity"]) is int and data["quantity"] == 1]
            for name in ["signedDate", "purchaseDate"]:
                valid.append(type(data[name]) is int and 0 < data[name] <= int(time.time() * 1000) + 300000)
            valid.append(data["revocationDate"] is None or (type(data["revocationDate"]) is int and data["revocationDate"] > 0))
            if not all(valid):
                raise ValueError("Invalid transaction fields")
        except (ValueError, TypeError) as error:
            raise StoreKitFailure("购买凭证与当前房源包不一致", 422) from error
        data["transactionId"] = str(data["transactionId"])
        return data

    async def transaction(self, transaction_id):
        if not self.settings.storekit_configured:
            raise StoreKitFailure("房源包暂未开放购买，请稍后再试")
        host = "https://api.storekit.apple.com" if self.settings.storekit_environment == "Production" else "https://api.storekit-sandbox.apple.com"
        try:
            response = await self.http.get(host + "/inApps/v1/transactions/" + transaction_id,
                                           headers={"Authorization": "Bearer " + self._authorize()})
            if response.status_code == 404:
                raise StoreKitFailure("该购买记录尚未确认，请稍后恢复容量", 409)
            if response.status_code != 200:
                raise StoreKitFailure()
            signed = response.json()["signedTransactionInfo"]
            decoded = await asyncio.to_thread(self.verifier.verify_and_decode_signed_transaction, signed)
        except StoreKitFailure:
            raise
        except Exception as error:
            # Do not surface JWTs, account tokens, credentials or upstream URLs.
            raise StoreKitFailure() from error
        result = self.validate(decoded)
        if result["transactionId"] != transaction_id:
            raise StoreKitFailure("购买记录标识不一致", 422)
        return result

    async def notification(self, signed):
        try:
            payload = await asyncio.to_thread(self.verifier.verify_and_decode_notification, signed)
            identity = str(uuid.UUID(payload.notificationUUID))
            data = payload.data
            transaction = None
            if data and data.signedTransactionInfo:
                decoded = await asyncio.to_thread(self.verifier.verify_and_decode_signed_transaction, data.signedTransactionInfo)
                if decoded.productId == self.settings.storekit_product_id:
                    transaction = self.validate(decoded)
            return identity, transaction
        except Exception as error:
            raise StoreKitFailure("通知校验失败", 401) from error


def install_storekit(app, settings, ledger, current_session, *, provider=None):
    apple = provider or AppleStoreKit(settings)
    locks = defaultdict(asyncio.Lock)
    attempts = defaultdict(deque)

    def rate(account):
        now = time.monotonic()
        if account not in attempts and len(attempts) >= 10000:
            raise StoreKitFailure("购买确认过于频繁，请稍后重试", 429)
        values = attempts[account]
        while values and values[0] < now - 60:
            values.popleft()
        if len(values) >= 30:
            raise StoreKitFailure("购买确认过于频繁，请稍后重试", 429)
        values.append(now)

    def require_configuration():
        if not settings.storekit_configured:
            raise StoreKitFailure("房源包暂未开放购买，请稍后再试")

    async def refresh(account):
        require_configuration()
        # Finished consumables were acknowledged only after durable server grant.
        # Their known IDs support recovery even on iOS17 without finished history.
        for identity in ledger.apple_ids(account, settings.storekit_environment):
            result = await apple.transaction(identity)
            try:
                ledger.apply_apple(account, result)
            except ValueError as error:
                raise StoreKitFailure("购买记录与当前账号不一致", 403) from error

    @app.exception_handler(StoreKitFailure)
    async def failure(_, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=error.status, content={"detail": {"code": "STOREKIT_UNCONFIRMED", "message": error.message}})

    @app.get("/api/rent-room/ios/storekit/context")
    async def context(session=Depends(current_session)):
        account = session["account"]
        rate(account)
        async with locks[account]:
            if settings.storekit_configured:
                await refresh(account)
            return {"available": settings.storekit_enabled and settings.storekit_configured,
                    "productId": settings.storekit_product_id or None, "packSize": 10,
                    "appAccountToken": ledger.apple_token(account),
                    **ledger.apple_snapshot(account, settings.storekit_environment)}

    @app.post("/api/rent-room/ios/storekit/verify")
    async def verify(body: TransactionRequest, session=Depends(current_session)):
        require_configuration()
        account = session["account"]
        rate(account)
        async with locks[account]:
            result = await apple.transaction(body.transactionId)
            try:
                return ledger.apply_apple(account, result)
            except ValueError as error:
                raise StoreKitFailure("请登录购买时使用的账号后恢复容量", 403) from error

    @app.post("/api/rent-room/ios/storekit/restore")
    async def restore(session=Depends(current_session)):
        account = session["account"]
        rate(account)
        async with locks[account]:
            await refresh(account)
            return ledger.apple_snapshot(account, settings.storekit_environment)

    @app.post("/api/rent-room/ios/storekit/notifications")
    async def notification(body: NotificationRequest):
        require_configuration()
        identity, transaction = await apple.notification(body.signedPayload)
        if transaction:
            account = ledger.apple_owner(transaction["appAccountToken"])
            if account:
                async with locks[account]:
                    # Re-query Apple so late notifications cannot resurrect refunds.
                    result = await apple.transaction(transaction["transactionId"])
                    try:
                        ledger.apply_apple(account, result, event_id=identity)
                    except ValueError as error:
                        raise StoreKitFailure("通知记录不一致", 409) from error
        return {"received": True}

    return apple
