from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.rent_room_credits import CreditWallets
from app.rent_room_main import create_app
from app.rent_room_store import RentRoomStore
from test_rent_room_capacity import FakeWeChat, checkout, login, notify_refund, query, settings


V3 = {"x-rent-room-capacity-version": "3"}


def migration(client, headers, active):
    return client.post("/api/rent-room/credits/migrate", headers={**headers, **V3}, json={"active": active})


def operation(client, headers, action, key="creation-request-01"):
    return client.post("/api/rent-room/credits/operations", headers={**headers, **V3},
                       json={"requestId": key, "action": action})


def test_old_and_new_devices_share_one_migration_without_changing_legacy_contract(settings):
    settings = settings.model_copy(update={"free_capacity": 2})
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, store=ledger, provider=provider)) as client:
        old = login(client)
        order = checkout(client, old).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, old).json() == {"status": "paid", "packs": 1, "limit": 12}
        new = client.post("/api/rent-room/session", headers=V3, json={"code": "A"}).json()
        assert new["credits"] == {"version": 1, "migrated": False}
        assert migration(client, old, 5).json()["credits"]["remaining"] == 7
        assert operation(client, old, "reserve").json()["credits"]["remaining"] == 6
        assert operation(client, old, "commit").json()["operation"]["state"] == "committed"
        # A second device cannot reset consumption or re-claim the initial grant.
        second = login(client)
        assert migration(client, second, 0).json()["credits"]["remaining"] == 6
        assert client.get("/api/rent-room/entitlements", headers=old).json() == {"packs": 1, "limit": 12}
        assert operation(client, second, "reserve").json()["credits"]["remaining"] == 6


def test_new_orders_are_points_only_and_refunds_never_remove_housing(settings):
    settings = settings.model_copy(update={"free_capacity": 2})
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, store=ledger, provider=provider)) as client:
        headers = login(client)
        assert checkout(client, {**headers, **V3}).status_code == 409
        assert migration(client, headers, 0).json()["credits"]["remaining"] == 2
        order = checkout(client, {**headers, **V3}).json()
        assert ledger.by_id(order["orderId"])["entitlement_kind"] == "credits"
        provider.remote[order["orderId"]] = {"status": 3}
        assert query(client, {**headers, **V3}).json()["credits"]["remaining"] == 12
        assert query(client, {**headers, **V3}).json()["credits"]["remaining"] == 12
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 0, "limit": 2}
        operation(client, headers, "reserve")
        operation(client, headers, "commit")
        assert notify_refund(client, settings, order["orderId"]).status_code == 200
        assert query(client, {**headers, **V3}).json()["credits"]["remaining"] == 1
        assert operation(client, headers, "cancel").json()["operation"]["state"] == "committed"
        assert migration(client, headers, 0).json()["credits"]["remaining"] == 1


def test_old_order_paid_after_upgrade_grants_once_and_keeps_purchase_semantics(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, store=ledger, provider=provider)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        assert migration(client, headers, 1).json()["credits"]["remaining"] == 0
        # Retrying checkout from another version cannot reinterpret an old order.
        assert checkout(client, {**headers, **V3}).json()["orderId"] == order["orderId"]
        assert ledger.by_id(order["orderId"])["entitlement_kind"] == "capacity"
        provider.remote[order["orderId"]] = {"status": 4}
        for _ in range(2):
            assert query(client, {**headers, **V3}).json()["credits"]["remaining"] == 10
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 1, "limit": 12}


def test_failed_creation_cancel_is_idempotent_and_blocks_late_reserve(settings):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, provider=FakeWeChat(settings))) as client:
        headers = login(client)
        migration(client, headers, 0)
        assert operation(client, headers, "reserve").json()["credits"]["remaining"] == 0
        assert operation(client, headers, "reserve", "creation-request-02").status_code == 409
        for _ in range(2):
            assert operation(client, headers, "cancel").json()["credits"]["remaining"] == 1
        assert operation(client, headers, "reserve").json()["operation"]["state"] == "cancelled"
        assert operation(client, headers, "commit").status_code == 409
        # Cancellation can arrive before a request whose response timed out.
        assert operation(client, headers, "cancel", "creation-request-03").status_code == 200
        assert operation(client, headers, "reserve", "creation-request-03").json()["credits"]["remaining"] == 1


def test_snapshot_and_operations_are_account_scoped_and_require_new_protocol(settings):
    with TestClient(create_app(settings, store=RentRoomStore(":memory:"), provider=FakeWeChat(settings))) as client:
        a, b = login(client), login(client, "B")
        assert client.post("/api/rent-room/credits/migrate", json={"active": 0}).status_code == 401
        assert client.post("/api/rent-room/credits/migrate", headers=a, json={"active": 0}).status_code == 409
        assert operation(client, a, "reserve").status_code == 409
        migration(client, a, 1)
        migration(client, b, 0)
        assert operation(client, a, "reserve").status_code == 409
        assert operation(client, b, "reserve").status_code == 200
        for active in [-1, 1.5, True, "0"]:
            assert migration(client, a, active).status_code == 422
        assert client.post("/api/rent-room/credits/migrate", headers={**a, **V3}, json={"active": 0, "remaining": 999}).status_code == 422


def test_database_reopen_and_concurrent_devices_never_spend_last_credit_twice(tmp_path):
    path = str(tmp_path / "wallet.sqlite3")
    a, b = RentRoomStore(path), RentRoomStore(path)
    wa, wb = CreditWallets(a), CreditWallets(b)
    try:
        assert wa.migrate("account", 0, 1)["remaining"] == 1
        assert wb.migrate("account", 0, 100)["remaining"] == 1
        def reserve(pair):
            wallet, key = pair
            try:
                return wallet.operate("account", key, "reserve")["operation"]["state"]
            except ValueError:
                return "insufficient"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [(wa, "device-a-creation"), (wb, "device-b-creation")]))
        assert sorted(results) == ["insufficient", "reserved"]
        assert wa.snapshot("account")["remaining"] == 0
    finally:
        a.close(); b.close()
    reopened = RentRoomStore(path)
    try:
        assert CreditWallets(reopened).migrate("account", 0, 2)["remaining"] == 0
    finally:
        reopened.close()


def test_catalog_v3_is_explicit_and_old_formats_do_not_change(settings):
    with TestClient(create_app(settings, store=RentRoomStore(":memory:"), provider=FakeWeChat(settings))) as client:
        current = client.get("/api/rent-room/catalog", headers=V3).json()
        assert current["creditVersion"] == 1
        for version in [None, "1", "2", "unknown"]:
            old = client.get("/api/rent-room/catalog", headers={"x-rent-room-capacity-version": version} if version else {}).json()
            assert "creditVersion" not in old


def test_over_capacity_migration_has_no_historical_debt_and_policy_changes_do_not_regrant(settings):
    ledger = RentRoomStore(":memory:")
    wallets = CreditWallets(ledger)
    assert wallets.migrate("account", 500, 2)["remaining"] == 0
    assert wallets.migrate("account", 0, 20)["remaining"] == 0
    order = ledger.create_order({"account": "account", "openid": "wx"}, "new-order", kind="credits")
    ledger.apply(order["id"], "paid", "wx-order")
    assert wallets.snapshot("account")["remaining"] == 10
    ledger.close()


def test_old_schema_migrates_additively_without_reinterpreting_paid_orders(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE rr_orders (id TEXT PRIMARY KEY, account TEXT NOT NULL, openid TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unpaid', wx_id TEXT UNIQUE, created INTEGER NOT NULL, last_checked INTEGER NOT NULL DEFAULT 0, UNIQUE(account,request_id))")
        db.execute("INSERT INTO rr_orders VALUES ('old','a','wx','old-request','paid','wx-old',1,0)")
    ledger = RentRoomStore(path)
    try:
        assert ledger.by_id("old")["entitlement_kind"] == "capacity"
        assert ledger.by_id("old")["price"] == 190
        assert CreditWallets(ledger).migrate("a", 5, 2)["remaining"] == 7
        assert ledger.packs("a") == 1
    finally:
        ledger.close()


def test_checkout_disabled_keeps_existing_wallet_and_creation_working(settings):
    settings = settings.model_copy(update={"payments_enabled": False})
    with TestClient(create_app(settings, store=RentRoomStore(":memory:"), provider=FakeWeChat(settings))) as client:
        headers = login(client)
        assert migration(client, headers, 0).json()["credits"]["remaining"] == 1
        assert operation(client, headers, "reserve").json()["credits"]["remaining"] == 0
        assert operation(client, headers, "commit").status_code == 200
        assert checkout(client, {**headers, **V3}).status_code == 503


def test_rate_guard_bounds_unknown_cancellations_but_allows_existing_settlement():
    ledger = RentRoomStore(":memory:")
    wallets = CreditWallets(ledger)
    try:
        wallets.migrate("a", 0, 1)
        wallets.operate("a", "creation-reserved", "reserve")
        for n in range(99):
            wallets.operate("a", f"cancel-{n:016d}", "cancel")
        with pytest.raises(ValueError, match="频繁"):
            wallets.operate("a", "another-cancellation", "cancel")
        assert wallets.operate("a", "creation-reserved", "commit")["operation"]["state"] == "committed"
        assert wallets.operate("a", "creation-reserved", "cancel")["credits"]["remaining"] == 0
    finally:
        ledger.close()


def test_apple_session_cannot_migrate_or_spend_wechat_wallet(settings):
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, store=ledger, provider=FakeWeChat(settings))) as client:
        token = ledger.create_session("apple-account", "apple", "", provider="apple")
        headers = {"Authorization": "Bearer " + token}
        assert migration(client, headers, 0).status_code == 409
        assert operation(client, headers, "reserve").status_code == 409
        assert CreditWallets(ledger).snapshot("apple-account") == {"version": 1, "migrated": False}
