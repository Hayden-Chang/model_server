import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_purchases import ApplePurchases, PurchaseFailure, PurchaseLedger, account_id

BASE = "/api/rent-room/ios/purchases/"
BUNDLE = "com.example.xunju"
PRODUCT = BUNDLE + ".capacity.10"


def identity(app_id="9001", env="Sandbox"):
    return {"account": account_id(BUNDLE, env, app_id), "environment": env, "appTransactionId": app_id}


def settings(path=":memory:"):
    return RentRoomSettings(_env_file=None, db_path=str(path), ios_bundle_id=BUNDLE, storekit_product_id=PRODUCT,
        storekit_app_apple_id=123, storekit_key_path="fixture-only", storekit_key_id="key", storekit_issuer_id="issuer", storekit_enabled=True)


def transaction(token, *, app_id="9001", env="Sandbox", tx="10001", revoked=None, signed=1000):
    return dict(transactionId=tx, bundleId=BUNDLE, productId=PRODUCT, appAccountToken=token,
        appTransactionId=app_id, environment=env, type="Consumable", inAppOwnershipType="PURCHASED", quantity=1,
        signedDate=signed, purchaseDate=500, revocationDate=revoked)


class FakeApple:
    def __init__(self):
        self.rows = {}; self.fail = False
        self.validator = ApplePurchases(RentRoomSettings(_env_file=None, ios_bundle_id=BUNDLE, storekit_product_id=PRODUCT))
    async def identity(self, proof):
        if proof not in ("signed-a", "signed-b", "signed-production"):
            raise PurchaseFailure("身份校验失败", 401)
        return identity("9002" if proof == "signed-b" else "9001", "Production" if proof == "signed-production" else "Sandbox")
    async def transaction(self, tx, owner, token):
        if self.fail: raise PurchaseFailure()
        return self.validator.validate_transaction(SimpleNamespace(**self.rows[owner["environment"], tx]), owner, token)
    async def close(self): await self.validator.close()


def post(client, path, proof="signed-a", **extra):
    return client.post(BASE + path, json={"signedAppTransaction": proof, **extra})


def test_disabled_and_bad_proofs_fail_closed_and_never_echo_credentials():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        assert post(client, "context").status_code == 503
        secret = "secret-proof" * 3000
        result = post(client, "context", secret)
        assert result.status_code == 422 and secret[:30] not in result.text
        assert post(client, "context", account="forged").status_code == 422
    fake = FakeApple()
    with TestClient(create_app(settings(), purchases=fake)) as client:
        assert post(client, "context", "forged").status_code == 401
        assert client.get(BASE + "context", headers={"Authorization": "Bearer app-login-token"}).status_code == 405
        assert post(client, "verify", transactionId="10001\n").status_code == 422


def test_duplicate_grant_restart_restore_and_refund_are_durable_without_app_sessions(tmp_path):
    db = tmp_path / "capacity.sqlite3"
    fake = FakeApple()
    with TestClient(create_app(settings(db), purchases=fake)) as client:
        context = post(client, "context").json()
        assert context["limit"] == 1 and context["available"] and context["capacity"]["freeLimit"] == 1
        token = context["appAccountToken"]
        fake.rows["Sandbox", "10001"] = transaction(token)
        def verify(_): return post(client, "verify", transactionId="10001")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(verify, range(4)))
        assert all(r.status_code == 200 and r.json()["packs"] == 1 and r.json()["revision"] == 1 for r in results)
        fake.fail = True
        assert post(client, "restore").status_code == 503
    reopened = FakeApple(); reopened.rows = copy.deepcopy(fake.rows)
    with TestClient(create_app(settings(db), purchases=reopened)) as client:
        restored = post(client, "restore").json()
        assert restored["packs"] == 1 and restored["limit"] == 11
        assert post(client, "context").json()["appAccountToken"] == token
        reopened.rows["Sandbox", "10001"].update(revocationDate=1400, signedDate=1500)
        assert post(client, "restore").json()["packs"] == 0
        reopened.rows["Sandbox", "10001"].update(revocationDate=None, signedDate=1000)
        stale = post(client, "verify", transactionId="10001").json()
        assert stale["packs"] == 0 and stale["status"] == "revoked" and stale["revision"] == 2
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM rr_sessions").fetchone()[0] == 0
    dump = "\n".join(conn.iterdump()); conn.close()
    assert "signed-a" not in dump and "signedAppTransaction" not in dump


def test_account_and_environment_isolation_and_disabled_sales_still_restore(tmp_path):
    config = settings(tmp_path / "capacity.sqlite3"); fake = FakeApple()
    with TestClient(create_app(config, purchases=fake)) as client:
        a = post(client, "context").json()
        b = post(client, "context", "signed-b").json()
        prod = post(client, "context", "signed-production").json()
        assert len({a["appAccountToken"], b["appAccountToken"], prod["appAccountToken"]}) == 3
        fake.rows["Sandbox", "10001"] = transaction(a["appAccountToken"])
        assert post(client, "verify", "signed-b", transactionId="10001").status_code == 403
        assert post(client, "verify", transactionId="10001").json()["packs"] == 1
        # Apple IDs can collide across environments; never combine the ledgers.
        fake.rows["Production", "10001"] = transaction(prod["appAccountToken"], env="Production")
        assert post(client, "verify", "signed-production", transactionId="10001").json()["packs"] == 1
        config.storekit_enabled = False
        assert not post(client, "context").json()["available"]
        assert post(client, "restore").json()["packs"] == 1
        assert post(client, "restore", "signed-b").json()["packs"] == 0


@pytest.mark.parametrize("field,bad", [
    ("bundleId", "other"), ("productId", "other"), ("appTransactionId", "9002"),
    ("appAccountToken", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), ("environment", "Production"),
    ("quantity", True), ("quantity", 2), ("type", "Non-Consumable"), ("inAppOwnershipType", "FAMILY_SHARED"),
    ("signedDate", -1), ("purchaseDate", True), ("revocationDate", -1), ("transactionId", "10001\n")])
def test_verified_transaction_fields_must_match_purchase_identity(field, bad):
    provider = ApplePurchases(RentRoomSettings(_env_file=None, ios_bundle_id=BUNDLE, storekit_product_id=PRODUCT))
    token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    value = transaction(token); value[field] = bad
    with pytest.raises(PurchaseFailure): provider.validate_transaction(SimpleNamespace(**value), identity(), token)
    asyncio.run(provider.close())


def test_apple_app_proof_uses_official_verifier_and_rejects_tampering(tmp_path):
    config = settings()
    key = tmp_path / "test.p8"; key.write_text("not-used-for-proof-verification")
    config.storekit_key_path = str(key)
    provider = ApplePurchases(config)
    forged = jwt.encode({"receiptType": "Sandbox", "bundleId": BUNDLE, "appTransactionId": "9001"}, "not-apple-signing-key-at-least-32-bytes", algorithm="HS256")
    with pytest.raises(PurchaseFailure) as failure: asyncio.run(provider.identity(forged))
    assert failure.value.status == 401 and forged not in str(failure.value.message)
    asyncio.run(provider.close())


def test_ledger_rejects_cross_account_replay_even_if_provider_is_incorrect():
    ledger = PurchaseLedger(":memory:")
    a, b = identity(), identity("9002")
    token = ledger.token(a); ledger.token(b)
    row = transaction(token)
    assert ledger.apply(a, row) == "verified"
    with pytest.raises(PurchaseFailure): ledger.apply(b, row)
    assert ledger.snapshot(b, 1)["packs"] == 0
    ledger.close()
