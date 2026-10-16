import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore
from app.rent_room_wechat import SecurePush, WeChatUnavailable, WeChatVirtualPay


@pytest.fixture
def settings():
    return RentRoomSettings(_env_file=None, app_secret="test-app-secret", offer_id="12345",
        app_key="test-virtual-payment-key", token_secret="independent-rental-session-secret-32-characters",
        push_token="test-push-token", push_aes_key=base64.b64encode(b"a" * 32).decode().rstrip("="),
        payments_enabled=True)


class FakeWeChat:
    def __init__(self, settings):
        self.signer = WeChatVirtualPay(settings)
        self.remote = {}
        self.queries = []
        self.deliveries = []

    async def close(self):
        await self.signer.close()

    async def login(self, code):
        return {"openid": "wx-" + code, "session_key": "private-session-key-" + code}

    def payment(self, order, key):
        return self.signer.payment(order, key)

    async def query(self, order):
        self.queries.append(order["id"])
        result = self.remote.get(order["id"], {"status": 1})
        if isinstance(result, Exception):
            raise result
        return {"order_id": order["id"], "order_type": 0, "paid_fee": 199, "order_fee": 199, "env_type": 1,
                "wx_order_id": "wx-order-" + order["id"], **result}

    async def delivered(self, order):
        self.deliveries.append(order["id"])


def login(client, code="A"):
    response = client.post("/api/rent-room/session", json={"code": code})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def checkout(client, headers, key="request-0001", **extra):
    return client.post("/api/rent-room/orders", headers=headers,
        json={"requestId": key, "productId": "room_capacity_10", **extra})


def query(client, headers, key="request-0001"):
    return client.get("/api/rent-room/orders/request/" + key, headers=headers)



def notify_refund(client, settings, order_id, **fields):
    push = SecurePush(settings)
    event = {"Event": "xpay_refund_notify", "OpenId": "wx-A", "MchOrderId": order_id,
             "WxOrderId": "wx-order-" + order_id, "MchRefundId": "refund-" + order_id,
             "WxRefundId": "wx-refund-" + order_id, "RetCode": 0, "RefundFee": 199, **fields}
    payload = push.encrypt(json.dumps(event).encode())
    params = {"timestamp": payload["TimeStamp"], "nonce": payload["Nonce"], "msg_signature": payload["MsgSignature"]}
    return client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": payload["Encrypt"]})

def test_disabled_catalog_and_checkout_fail_closed():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        catalog = client.get("/api/rent-room/catalog")
        assert catalog.json() == {"available": False, "iosAvailable": False,
            "product": {"id": "room_capacity_10", "price": 199, "size": 10}}
        assert client.post("/api/rent-room/session", json={"code": "A"}).status_code == 503
        assert client.get("/health/live").status_code == 200
        assert catalog.headers["cache-control"] == "no-store"


def test_configuration_requires_independent_secrets_and_tolerates_empty_compose_values():
    assert not RentRoomSettings(_env_file=None, token_secret="", push_token="", push_aes_key="").login_ready
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, payments_enabled=True)
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, token_secret="x" * 32, app_key="x" * 32)


def test_authentication_account_isolation_and_session_key_encryption(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        assert client.get("/api/rent-room/entitlements").status_code == 401
        assert client.get("/api/rent-room/entitlements", headers={"Authorization": "Bearer timefragment-token"}).status_code == 401
        a, b = login(client), login(client, "B")
        row = ledger.db.execute("SELECT * FROM rr_sessions LIMIT 1").fetchone()
        assert "private-session-key" not in row["session_key"]
        order = checkout(client, a).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, b).json() == {"status": "missing", "packs": 0, "limit": 2}
        assert query(client, a).json() == {"status": "paid", "packs": 1, "limit": 12}
        assert "private-session-key" not in json.dumps(order)


def test_order_idempotency_stacking_refund_and_stale_paid_results(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        first = checkout(client, headers).json()
        assert checkout(client, headers).json()["orderId"] == first["orderId"]
        assert query(client, headers).json()["packs"] == 0
        provider.remote[first["orderId"]] = {"status": 2}
        assert query(client, headers).json()["limit"] == 12
        assert query(client, headers).json()["packs"] == 1
        second = checkout(client, headers, "request-0002").json()
        provider.remote[second["orderId"]] = {"status": 4}
        assert query(client, headers, "request-0002").json()["limit"] == 22
        notify_refund(client, settings, first["orderId"])
        assert query(client, headers).json()["limit"] == 12
        ledger.apply(first["orderId"], "paid", "late-payment")
        assert query(client, headers).json()["packs"] == 1


@pytest.mark.parametrize("remote", [
    {"status": 2, "paid_fee": 1}, {"status": 4, "wx_order_id": ""},
    {"status": 2, "order_id": "other-order"}, {"status": 2, "order_type": 1},
    {"status": 999}, WeChatUnavailable("upstream unavailable")])
def test_mismatched_unknown_or_failed_queries_never_grant_capacity(settings, remote):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = remote
        assert query(client, headers).status_code == 503
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 0


def test_platform_payment_identity_cannot_grant_two_orders(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        for index in (1, 2):
            key = "request-000" + str(index)
            order = checkout(client, headers, key).json()
            provider.remote[order["orderId"]] = {"status": 2, "wx_order_id": "one-payment"}
            assert query(client, headers, key).status_code == (200 if index == 1 else 503)
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 1, "limit": 12}
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1


def test_checkout_rejects_price_tampering_unconfigured_ios_and_cross_product_tokens(settings):
    with TestClient(create_app(settings, provider=FakeWeChat(settings))) as client:
        headers = login(client)
        assert checkout(client, headers, price=1).status_code == 422
        assert checkout(client, headers, platform="ios").status_code == 503
        assert checkout(client, {"Authorization": "Bearer wrong"}).status_code == 401
        assert checkout(client, headers).status_code == 200


def test_disabling_new_checkout_still_reconciles_existing_purchases_and_refunds(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        settings.payments_enabled = False
        assert client.get("/api/rent-room/catalog").json()["available"] is False
        assert checkout(client, headers, "request-0002").status_code == 503
        notify_refund(client, settings, order["orderId"])
        assert query(client, headers).json() == {"status": "refunded", "packs": 0, "limit": 2}


def test_wechat_signatures_match_exact_bytes_and_query_authentication(settings):
    calls = []
    def transport(request):
        calls.append(request)
        if request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "server-only-access-token", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "order": {"status": 1}})
    async def run():
        provider = WeChatVirtualPay(settings, httpx.MockTransport(transport))
        order = {"id": "a" * 32, "openid": "wx-A", "product_id": "room_capacity_10", "price": 199}
        payment = provider.payment(order, "session-key")
        assert json.loads(payment["signData"]) == {"offerId": "12345", "buyQuantity": 1, "env": 0,
            "currencyType": "CNY", "productId": "room_capacity_10", "goodsPrice": 199,
            "outTradeNo": "a" * 32, "attach": "a" * 32}
        expected = hmac.new(b"test-virtual-payment-key", ("requestVirtualPayment&" + payment["signData"]).encode(), hashlib.sha256).hexdigest()
        assert payment["paySig"] == expected
        assert payment["signature"] == hmac.new(b"session-key", payment["signData"].encode(), hashlib.sha256).hexdigest()
        await provider.query(order)
        request = calls[-1]
        assert json.loads(request.content) == {"openid": "wx-A", "env": 0, "order_id": "a" * 32}
        assert request.url.params["pay_sig"] == hmac.new(b"test-virtual-payment-key", b"/xpay/query_order&" + request.content, hashlib.sha256).hexdigest()
        assert request.url.params["access_token"] == "server-only-access-token"
        await provider.close()
    asyncio.run(run())


def test_secure_notifications_verify_recipient_signature_and_replay_safely(settings):
    ledger, provider, push = RentRoomStore(":memory:"), FakeWeChat(settings), SecurePush(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        payload = push.encrypt(json.dumps({"Event": "xpay_goods_deliver_notify", "OpenId": "wx-A", "Env": 0, "OutTradeNo": order["orderId"]}).encode())
        params = {"timestamp": payload["TimeStamp"], "nonce": payload["Nonce"], "msg_signature": payload["MsgSignature"]}
        for _ in range(2):
            response = client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": payload["Encrypt"]})
            assert response.status_code == 200
        assert provider.deliveries == []  # The encrypted callback reply ACKs delivery.
        assert query(client, headers).json()["packs"] == 1
        params["msg_signature"] = "wrong"
        assert client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": payload["Encrypt"]}).status_code == 403
        wrong_settings = settings.model_copy(update={"app_id": "other-app"})
        other = SecurePush(wrong_settings).encrypt(b"{}")
        params = {"timestamp": other["TimeStamp"], "nonce": other["Nonce"], "msg_signature": other["MsgSignature"]}
        assert client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": other["Encrypt"]}).status_code == 403


def test_ledger_survives_restart_and_serializes_multiple_connections(tmp_path):
    path = str(tmp_path / "capacity.sqlite3")
    a, b = RentRoomStore(path), RentRoomStore(path)
    session = {"account": "A", "openid": "wx-A"}
    with ThreadPoolExecutor(2) as executor:
        orders = list(executor.map(lambda store: store.create_order(session, "same-request"), [a, b]))
    assert orders[0]["id"] == orders[1]["id"]
    a.apply(orders[0]["id"], "paid", "wx-order")
    b.apply(orders[0]["id"], "refunded")
    a.apply(orders[0]["id"], "paid", "wx-order")
    a.close(); b.close()
    reloaded = RentRoomStore(path)
    assert reloaded.packs("A") == 0
    assert reloaded.by_id(orders[0]["id"])["state"] == "refunded"
    reloaded.close()


def test_secure_xml_notifications_accept_cdata_but_reject_entity_declarations(settings):
    ledger, provider, push = RentRoomStore(":memory:"), FakeWeChat(settings), SecurePush(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        event = ("<xml><OpenId>wx-A</OpenId><Env>0</Env><Event><![CDATA[xpay_goods_deliver_notify]]></Event><OutTradeNo><![CDATA["
                 + order["orderId"] + "]]></OutTradeNo></xml>").encode()
        payload = push.encrypt(event)
        params = {"timestamp": payload["TimeStamp"], "nonce": payload["Nonce"], "msg_signature": payload["MsgSignature"]}
        envelope = "<xml><Encrypt><![CDATA[" + payload["Encrypt"] + "]]></Encrypt></xml>"
        response = client.post("/api/rent-room/wechat/notify", params=params, content=envelope,
                               headers={"content-type": "application/xml"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        assert query(client, headers).json()["packs"] == 1
        dangerous = '<!DOCTYPE xml [<!ENTITY injected "payload">]><xml><Encrypt>&injected;</Encrypt></xml>'
        assert client.post("/api/rent-room/wechat/notify", params=params, content=dangerous).status_code == 403


def test_caddy_routes_capacity_before_timefragment_accounts():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for filename in ("Caddyfile", "Caddyfile.accounts"):
        text = (root / filename).read_text()
        assert "reverse_proxy rent-room-api:8000" in text
        assert text.index("handle /api/rent-room/*") < text.index("reverse_proxy business-api:8000")
    account = (root / "Caddyfile.accounts").read_text()
    assert account.index("handle /api/rent-room/*") < account.index("@account path /api/*")


def test_optional_rental_service_cannot_block_existing_gateway_startup():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    assert "rent-room-api" not in compose["services"]["caddy"]["depends_on"]
    assert "ports" not in compose["services"]["rent-room-api"]
    assert compose["services"]["rent-room-api"]["volumes"] == ["rent-room-capacity:/var/lib/rent-room"]


def test_shared_session_contract_works_with_payments_disabled_and_expires():
    settings = RentRoomSettings(_env_file=None, app_secret="test-secret",
        token_secret="independent-session-encryption-secret-for-test")
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        first = client.post("/api/rent-room/session", json={"code": "A"}).json()
        second = client.post("/api/rent-room/session", json={"code": "A"}).json()
        assert set(first) == {"account", "token", "packs", "limit"}
        assert first["account"] == second["account"] == hashlib.sha256((settings.app_id + ":wx-A").encode()).hexdigest()
        assert 32 <= len(first["token"]) <= 256 and first["token"] != second["token"]
        assert first["packs"] == 0 and first["limit"] == 2
        headers = {"Authorization": "Bearer " + first["token"]}
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 0, "limit": 2}
        assert client.get("/api/rent-room/catalog").json()["available"] is False
        assert checkout(client, headers).status_code == 503
        assert client.post("/api/rent-room/session", json={"code": "A", "account": "forged"}).status_code == 422
        ledger.db.execute("UPDATE rr_sessions SET expires=0")
        ledger.db.commit()
        assert client.get("/api/rent-room/entitlements", headers=headers).status_code == 401


@pytest.mark.parametrize("upstream", [{"errcode": 40029}, {}, {"openid": "wx-A"}])
def test_failed_wechat_login_never_issues_a_session(settings, upstream):
    provider = WeChatVirtualPay(settings, httpx.MockTransport(lambda _: httpx.Response(200, json=upstream)))
    ledger = RentRoomStore(":memory:")
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        response = client.post("/api/rent-room/session", json={"code": "invalid"})
        assert response.status_code == 503
        assert ledger.db.execute("SELECT count(*) FROM rr_sessions").fetchone()[0] == 0
        assert "test-app-secret" not in response.text


def test_login_reconciles_a_missed_payment_before_returning_capacity(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        restored = client.post("/api/rent-room/session", json={"code": "A"})
        assert restored.json()["limit"] == 12
        assert provider.deliveries == [order["orderId"]]


def test_delivery_notification_cannot_acknowledge_an_unconfirmed_payment(settings):
    ledger, provider, push = RentRoomStore(":memory:"), FakeWeChat(settings), SecurePush(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        order = checkout(client, login(client)).json()
        payload = push.encrypt(json.dumps({"Event": "xpay_goods_deliver_notify", "OpenId": "wx-A", "Env": 0, "OutTradeNo": order["orderId"]}).encode())
        params = {"timestamp": payload["TimeStamp"], "nonce": payload["Nonce"], "msg_signature": payload["MsgSignature"]}
        response = client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": payload["Encrypt"]})
        assert response.status_code == 503
        assert ledger.by_id(order["orderId"])["state"] == "unpaid"


def test_catalog_defaults_and_orders_use_fixed_production_contract(settings):
    assert RentRoomSettings(_env_file=None).offer_id == "1450681249"
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        assert client.post("/api/rent-room/orders", headers=headers, json={
            "requestId": "wrong-product", "productId": "house-capacity-10"}).status_code == 422
        order = checkout(client, headers).json()
        row = ledger.by_id(order["orderId"])
        assert (row["product_id"], row["price"], row["pack_size"]) == ("room_capacity_10", 199, 10)
        assert checkout(client, headers).json()["payment"] == order["payment"]


def test_migration_preserves_legacy_price_and_new_orders_use_199(tmp_path, settings):
    path = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE rr_orders (id TEXT PRIMARY KEY, account TEXT NOT NULL, openid TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unpaid', wx_id TEXT UNIQUE, created INTEGER NOT NULL, last_checked INTEGER NOT NULL DEFAULT 0, UNIQUE(account,request_id))")
        account = hashlib.sha256((settings.app_id + ":wx-A").encode()).hexdigest()
        db.execute("INSERT INTO rr_orders(id,account,openid,request_id,created) VALUES (?,?,?,?,?)", ("old-order", account, "wx-A", "old-request", 1))
    ledger, provider = RentRoomStore(path), FakeWeChat(settings)
    provider.remote["old-order"] = {"status": 2, "paid_fee": 190, "order_fee": 190}
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        assert query(client, headers, "old-request").json()["limit"] == 12
        legacy = ledger.by_id("old-order")
        assert legacy["price"] == 190 and legacy["product_id"] == "house-capacity-10"
        assert json.loads(provider.payment(legacy, "key")["signData"])["goodsPrice"] == 190
        new = checkout(client, headers).json()
        assert json.loads(new["payment"]["signData"])["goodsPrice"] == 199
    reloaded = RentRoomStore(path)
    assert reloaded.packs(account) == 1
    assert reloaded.by_id("old-order")["price"] == 190
    reloaded.close()


def test_background_reconciliation_fulfills_without_any_client_poll_or_callback(settings):
    ledger, provider, delivered = RentRoomStore(":memory:"), FakeWeChat(settings), threading.Event()
    original = provider.delivered
    async def delivery(order):
        await original(order)
        delivered.set()
    provider.delivered = delivery
    with TestClient(create_app(settings, provider=provider, store=ledger, reconcile_interval=0.02)) as client:
        order = checkout(client, login(client)).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert delivered.wait(2), "Background reconciler did not deliver"
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1


def test_bounded_due_batches_visit_older_orders_and_throttle_stale_paid_results():
    ledger = RentRoomStore(":memory:")
    for n in range(14):
        ledger.create_order({"account": str(n), "openid": "wx-" + str(n)}, "same-request")
    first = ledger.orders(limit=10)
    assert len(first) == 10
    for order in first:
        ledger.checked(order["id"])
    second = ledger.orders(limit=10)
    assert len(second) == 4 and not {o["id"] for o in first} & {o["id"] for o in second}
    order = first[0]
    ledger.apply(order["id"], "paid", "wx-paid")
    ledger.db.execute("UPDATE rr_orders SET last_checked=0 WHERE id=?", (order["id"],))
    ledger.db.commit()
    ledger.apply(order["id"], "unpaid")
    assert ledger.by_id(order["id"])["state"] == "paid"
    assert ledger.by_id(order["id"])["last_checked"] > 0
    ledger.close()


def test_interrupted_queries_retry_soon_without_starving_unchecked_orders(monkeypatch):
    clock = 1000000
    monkeypatch.setattr("app.rent_room_store.time.time", lambda: clock)
    ledger = RentRoomStore(":memory:")
    first = ledger.create_order({"account": "A", "openid": "wx-A"}, "first-request")
    second = ledger.create_order({"account": "B", "openid": "wx-B"}, "second-request")
    ledger.checked(first["id"], retry_after=30)
    assert [r["id"] for r in ledger.orders()] == [second["id"]]
    clock += 31
    assert [r["id"] for r in ledger.orders()] == [second["id"], first["id"]]
    ledger.close()


def test_secure_push_signature_matches_the_published_wechat_vectors():
    push = object.__new__(SecurePush)
    push.token = "AAAAA"  # Public documentation vector, not a configured token.
    assert push.signature("1714036504", "1514711492") == "f464b24fc39322e44b38aa78f5edd27bd1441696"
    assert push.signature("1714037059", "486452656") == "899cf89e464efb63f54ddac96b0a0a235f53aa78"


def test_verified_refunds_are_idempotent_and_ignore_stale_paid_queries(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        for _ in range(2):
            assert notify_refund(client, settings, order["orderId"]).status_code == 200
        assert query(client, headers).json() == {"status": "refunded", "packs": 0, "limit": 2}
        assert ledger.db.execute("SELECT count(*) FROM rr_refunds").fetchone()[0] == 1
        restored = client.post("/api/rent-room/session", json={"code": "A"}).json()
        assert restored["limit"] == 2


@pytest.mark.parametrize("fields", [
    {"WxOrderId": "wrong-payment"}, {"OpenId": "wx-other"}, {"MchRefundId": ""},
    {"WxRefundId": ""}, {"OpenId": None}, {"RetCode": None}, {"RefundFee": 200}, {"RefundFee": True}])
def test_invalid_authenticated_refunds_never_revoke_capacity(settings, fields):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        assert notify_refund(client, settings, order["orderId"], **fields).status_code == 409
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1


def test_failed_partial_and_cumulative_full_refunds(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        assert notify_refund(client, settings, order["orderId"], RetCode=1).status_code == 200
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1
        for index, fee in enumerate((100, 99)):
            assert notify_refund(client, settings, order["orderId"], MchRefundId=f"partial-{index}", WxRefundId=f"wx-partial-{index}", RefundFee=fee).status_code == 200
            assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == (1 if index == 0 else 0)


def test_refund_before_local_fulfillment_links_platform_payment_without_granting(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 5, "left_fee": 0}
        assert notify_refund(client, settings, order["orderId"]).status_code == 200
        assert query(client, headers).json()["status"] == "refunded"
        assert provider.deliveries == []


def test_payment_status_alone_does_not_guess_refund_amount(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        provider.remote[order["orderId"]] = {"status": 5}
        assert query(client, headers).json()["packs"] == 1


@pytest.mark.parametrize("environment", [{"Env": 1}, {"env": 1}, {"Env": None}])
def test_explicit_nonproduction_refund_events_are_rejected(settings, environment):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        assert notify_refund(client, settings, order["orderId"], **environment).status_code == 403
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1


def test_a_known_payment_identity_cannot_change_during_reconciliation(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        provider.remote[order["orderId"]] = {"status": 8, "left_fee": 0, "wx_order_id": "different-payment"}
        assert query(client, headers).status_code == 503
        assert ledger.by_id(order["orderId"])["wx_id"] == "wx-order-" + order["orderId"]


@pytest.mark.parametrize("remote", [
    {"status": 5, "left_fee": 0}, {"status": 8, "left_fee": 99},
    {"status": 8}, {"status": 8, "left_fee": None},
    {"status": 8, "left_fee": "0"}, {"status": 8, "left_fee": False}])
def test_incomplete_partial_or_untyped_refunds_preserve_existing_capacity(settings, remote):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        provider.remote[order["orderId"]] = remote
        assert query(client, headers).json()["packs"] == 1


@pytest.mark.parametrize("fields", [{"order_fee": 190}, {"paid_fee": 190}, {"env_type": 2}])
def test_refund_query_rejects_wrong_snapshot_amount_or_environment(settings, fields):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 1
        provider.remote[order["orderId"]] = {"status": 8, "left_fee": 0, **fields}
        assert query(client, headers).status_code == 503
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 1


def test_full_refund_query_recovers_a_missed_notification_and_late_events_are_idempotent(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["limit"] == 12
        provider.remote[order["orderId"]] = {"status": 8, "left_fee": 0}
        for _ in range(2):
            assert query(client, headers).json() == {"status": "refunded", "packs": 0, "limit": 2}
        assert notify_refund(client, settings, order["orderId"]).status_code == 200
        provider.remote[order["orderId"]] = {"status": 2}
        assert query(client, headers).json()["packs"] == 0
