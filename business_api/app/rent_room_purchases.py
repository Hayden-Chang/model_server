"""Account-free native capacity: Apple app identity + durable consumable ledger.

No house data, app sessions, WeChat identities or client-supplied account IDs enter
this service. Signed app proofs are credentials; never persist or log them.
"""
import asyncio
import hashlib
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .appstore_client import build_client_token, APPLE_PRODUCTION_URL, APPLE_SANDBOX_URL


class PurchaseFailure(Exception):
    def __init__(self, message="购买记录暂时无法确认，请稍后重试", status=503):
        self.message, self.status = message, status


class AppProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signedAppTransaction: str = Field(min_length=1, max_length=24000, repr=False)


class VerifyPurchase(AppProof):
    transactionId: str = Field(pattern=r"^[0-9]{1,32}$")


def account_id(bundle, environment, app_transaction):
    return hashlib.sha256(f"rent-room-app-store-v1:{bundle}:{environment}:{app_transaction}".encode()).hexdigest()


def raw(value):
    return getattr(value, "value", value)


class ApplePurchases:
    def __init__(self, settings, *, http=None, verifiers=None):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=10)
        self.verifiers = verifiers or {}
        self.credential = None
        if settings.storekit_configured:
            from appstoreserverlibrary.models.Environment import Environment
            from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding
            roots = x509.load_pem_x509_certificates(Path(__file__).with_name("apple_root_ca_g3.pem").read_bytes())
            for environment in ("Production", "Sandbox"):
                self.verifiers.setdefault(environment, SignedDataVerifier(
                    [c.public_bytes(Encoding.DER) for c in roots], True, Environment(environment),
                    settings.ios_bundle_id, settings.storekit_app_apple_id))
            self.key = Path(settings.storekit_key_path).read_bytes()

    async def close(self):
        await self.http.aclose()

    async def identity(self, signed):
        try:
            # Untrusted payload chooses a verifier only. No claims are used until
            # that verifier validates Apple certificate chain, bundle and environment.
            environment = jwt.decode(signed, options={"verify_signature": False})["receiptType"]
            verifier = self.verifiers[environment]
            value = await asyncio.to_thread(verifier.verify_and_decode_app_transaction, signed)
            app_id = value.appTransactionId
            if (not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,32}", app_id)
                    or value.bundleId != self.settings.ios_bundle_id
                    or raw(value.receiptType) != environment
                    or environment not in ("Production", "Sandbox")
                    or environment == "Production" and value.appAppleId != self.settings.storekit_app_apple_id):
                raise ValueError("app identity mismatch")
            return {"account": account_id(value.bundleId, environment, app_id),
                    "environment": environment, "appTransactionId": app_id}
        except Exception as error:
            raise PurchaseFailure("App Store 购买身份校验失败，请恢复容量重试", 401) from error

    def authorize(self):
        if not self.credential or time.monotonic() - self.credential[1] > 240:
            token = build_client_token(key_p8=self.key, key_id=self.settings.storekit_key_id,
                issuer_id=self.settings.storekit_issuer_id, bundle_id=self.settings.ios_bundle_id)
            self.credential = token, time.monotonic()
        return self.credential[0]

    def validate_transaction(self, value, identity, token):
        data = {name: raw(getattr(value, name, None)) for name in (
            "transactionId", "bundleId", "productId", "appAccountToken", "appTransactionId", "environment",
            "type", "inAppOwnershipType", "quantity", "signedDate", "purchaseDate", "revocationDate")}
        try:
            valid = [isinstance(data["transactionId"], str) and re.fullmatch(r"[0-9]{1,32}", data["transactionId"]),
                data["bundleId"] == self.settings.ios_bundle_id,
                data["productId"] == self.settings.storekit_product_id,
                data["environment"] == identity["environment"],
                data["appTransactionId"] == identity["appTransactionId"],
                str(uuid.UUID(str(data["appAccountToken"]))) == token,
                data["type"] == "Consumable", data["inAppOwnershipType"] == "PURCHASED",
                type(data["quantity"]) is int and data["quantity"] == 1]
            now = int(time.time() * 1000) + 300000
            valid += [type(data[key]) is int and 0 < data[key] <= now for key in ("signedDate", "purchaseDate")]
            valid.append(data["revocationDate"] is None or type(data["revocationDate"]) is int
                         and 0 < data["revocationDate"] <= now)
            if not all(valid):
                raise ValueError("transaction mismatch")
        except (ValueError, TypeError) as error:
            raise PurchaseFailure("购买记录与当前 App Store 账号或房源包不一致", 403) from error
        return data

    async def transaction(self, transaction_id, identity, token):
        environment = identity["environment"]
        host = APPLE_PRODUCTION_URL if environment == "Production" else APPLE_SANDBOX_URL
        try:
            response = await self.http.get(host + "/inApps/v1/transactions/" + transaction_id,
                headers={"Authorization": "Bearer " + self.authorize()})
            if response.status_code == 404:
                raise PurchaseFailure("该购买记录尚未确认，请稍后恢复容量", 409)
            if response.status_code != 200:
                raise PurchaseFailure()
            decoded = await asyncio.to_thread(self.verifiers[environment].verify_and_decode_signed_transaction,
                                               response.json()["signedTransactionInfo"])
            result = self.validate_transaction(decoded, identity, token)
            if result["transactionId"] != transaction_id:
                raise PurchaseFailure("购买记录标识不一致", 422)
            return result
        except PurchaseFailure:
            raise
        except Exception as error:
            raise PurchaseFailure() from error


class PurchaseLedger:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS rr_native_accounts (
                account TEXT PRIMARY KEY, environment TEXT NOT NULL, app_id TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS rr_native_purchases (
                environment TEXT NOT NULL, id TEXT NOT NULL, account TEXT NOT NULL,
                product TEXT NOT NULL, purchased INTEGER NOT NULL, signed INTEGER NOT NULL,
                revoked INTEGER, PRIMARY KEY(environment, id));
            CREATE INDEX IF NOT EXISTS rr_native_owner ON rr_native_purchases(account);
        """)

    def close(self):
        self.db.close()

    def token(self, identity):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO rr_native_accounts(account, environment, app_id, token) VALUES(?,?,?,?)",
                            (identity["account"], identity["environment"], identity["appTransactionId"], str(uuid.uuid4())))
            row = self.db.execute("SELECT * FROM rr_native_accounts WHERE account=?", (identity["account"],)).fetchone()
            if row["environment"] != identity["environment"] or row["app_id"] != identity["appTransactionId"]:
                raise PurchaseFailure("购买身份不一致", 403)
            return row["token"]

    def snapshot(self, identity, free):
        with self.lock:
            packs = self.db.execute("SELECT COUNT(*) FROM rr_native_purchases WHERE account=? AND revoked IS NULL", (identity["account"],)).fetchone()[0]
            revision = self.db.execute("SELECT revision FROM rr_native_accounts WHERE account=?", (identity["account"],)).fetchone()[0]
            return {"account": identity["account"], "environment": identity["environment"], "packs": packs,
                    "limit": free + packs * 10, "revision": revision, "capacity": {"version": 2, "freeLimit": free}}

    def ids(self, identity):
        with self.lock:
            return [row[0] for row in self.db.execute("SELECT id FROM rr_native_purchases WHERE account=?", (identity["account"],))]

    def apply(self, identity, value):
        with self.lock, self.db:
            # One transaction serializes duplicate requests even across workers.
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT * FROM rr_native_purchases WHERE environment=? AND id=?",
                                  (identity["environment"], value["transactionId"])).fetchone()
            if old and (old["account"] != identity["account"] or old["product"] != value["productId"] or old["purchased"] != value["purchaseDate"]):
                raise PurchaseFailure("购买记录已关联其他身份或内容不一致", 403)
            if old is None:
                self.db.execute("INSERT INTO rr_native_purchases VALUES(?,?,?,?,?,?,?)", (
                    identity["environment"], value["transactionId"], identity["account"], value["productId"],
                    value["purchaseDate"], value["signedDate"], value["revocationDate"]))
                changed = True
            elif value["signedDate"] > old["signed"]:
                # Refunds are terminal for this consumable. A stale grant can never
                # resurrect refunded capacity; user records remain local and intact.
                revoked = old["revoked"] or value["revocationDate"]
                changed = (old["revoked"] is None) != (revoked is None)
                self.db.execute("UPDATE rr_native_purchases SET signed=?, revoked=? WHERE environment=? AND id=?",
                                (value["signedDate"], revoked, identity["environment"], value["transactionId"]))
            else:
                changed = False
                if value["signedDate"] == old["signed"] and value["revocationDate"] != old["revoked"]:
                    raise PurchaseFailure("购买记录版本不一致", 409)
            if changed:
                self.db.execute("UPDATE rr_native_accounts SET revision=revision+1 WHERE account=?", (identity["account"],))
            row = self.db.execute("SELECT revoked FROM rr_native_purchases WHERE environment=? AND id=?", (identity["environment"], value["transactionId"])).fetchone()
            return "revoked" if row[0] is not None else "verified"


class PurchaseService:
    def __init__(self, settings, provider, ledger):
        self.settings, self.apple, self.ledger = settings, provider, ledger
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.attempts = OrderedDict()
        self.proof_slots = asyncio.Semaphore(8)

    def rate(self, key):
        now = time.monotonic()
        values = self.attempts.setdefault(key, deque())
        self.attempts.move_to_end(key)
        while values and values[0] < now - 60:
            values.popleft()
        if len(values) >= 30:
            raise PurchaseFailure("购买确认过于频繁，请稍后重试", 429)
        values.append(now)
        while len(self.attempts) > 10000:
            self.attempts.popitem(last=False)

    async def identity(self, body, request):
        if not self.settings.storekit_configured:
            raise PurchaseFailure("房源包暂未开放购买，请稍后再试")
        # Peer address only: callers cannot bypass limits with forwarded headers.
        self.rate("ip:" + (request.client.host if request.client else "unknown"))
        async with self.proof_slots:
            identity = await self.apple.identity(body.signedAppTransaction)
        self.rate("account:" + identity["account"])
        return identity

    def lock(self, identity):
        return self.locks[int(identity["account"][:2], 16) % len(self.locks)]

    async def refresh(self, identity, token):
        for transaction_id in self.ledger.ids(identity):
            self.ledger.apply(identity, await self.apple.transaction(transaction_id, identity, token))

    async def close(self):
        await self.apple.close()
        self.ledger.close()


def install_purchases(app, settings, *, provider=None):
    service = PurchaseService(settings, provider or ApplePurchases(settings), PurchaseLedger(settings.db_path))

    @app.exception_handler(PurchaseFailure)
    async def failure(_, error):
        return JSONResponse(status_code=error.status, content={"detail": {"code": "PURCHASE_UNCONFIRMED", "message": error.message}})

    # FastAPI's normal validation output includes request values. Never echo a
    # signed credential in errors, including overlong or malformed proof fields.
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    previous_validation_handler = app.exception_handlers.get(RequestValidationError, request_validation_exception_handler)
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        if not request.url.path.startswith("/api/rent-room/ios/purchases/"):
            return await previous_validation_handler(request, error)
        return JSONResponse(status_code=422, content={"detail": {"code": "INVALID_REQUEST", "message": "请求格式无效"}})

    @app.post("/api/rent-room/ios/purchases/context")
    async def context(body: AppProof, request: Request):
        identity = await service.identity(body, request)
        async with service.lock(identity):
            token = service.ledger.token(identity)
            await service.refresh(identity, token)
            return {"available": settings.storekit_enabled, "productId": settings.storekit_product_id,
                    "packSize": 10, "appAccountToken": token, **service.ledger.snapshot(identity, settings.free_capacity)}

    @app.post("/api/rent-room/ios/purchases/verify")
    async def verify(body: VerifyPurchase, request: Request):
        identity = await service.identity(body, request)
        async with service.lock(identity):
            token = service.ledger.token(identity)
            value = await service.apple.transaction(body.transactionId, identity, token)
            status = service.ledger.apply(identity, value)
            return {"transactionId": body.transactionId, "status": status, **service.ledger.snapshot(identity, settings.free_capacity)}

    @app.post("/api/rent-room/ios/purchases/restore")
    async def restore(body: AppProof, request: Request):
        identity = await service.identity(body, request)
        async with service.lock(identity):
            token = service.ledger.token(identity)
            await service.refresh(identity, token)
            return service.ledger.snapshot(identity, settings.free_capacity)

    return service
