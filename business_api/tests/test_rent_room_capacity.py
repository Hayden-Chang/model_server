import asyncio
import base64
import hashlib
import hmac
import json
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
        return {"order_id": order["id"], "order_type": 0, "paid_fee": 190,
                "wx_order_id": "wx-order-" + order["id"], **result}

    async def delivered(self, order):
        self.deliveries.append(order["id"])


def login(client, code="A"):
    response = client.post("/api/rent-room/session", json={"code": code})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def checkout(client, headers, key="request-0001", **extra):
    return client.post("/api/rent-room/orders", headers=headers,
        json={"requestId": key, "productId": "house-capacity-10", **extra})


def query(client, headers, key="request-0001"):
    return client.get("/api/rent-room/orders/request/" + key, headers=headers)


def test_disabled_catalog_and_checkout_fail_closed():
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        catalog = client.get("/api/rent-room/catalog")
        assert catalog.json() == {"available": False, "iosAvailable": False,
            "product": {"id": "house-capacity-10", "price": 190, "size": 10}}
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
        provider.remote[first["orderId"]] = {"status": 5}
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
        provider.remote[order["orderId"]] = {"status": 5}
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
        order = {"id": "a" * 32, "openid": "wx-A"}
        payment = provider.payment(order, "session-key")
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
        payload = push.encrypt(json.dumps({"Event": "xpay_goods_deliver_notify", "OutTradeNo": order["orderId"]}).encode())
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


def test_caddy_routes_capacity_before_timefragment_accounts():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for filename in ("Caddyfile", "Caddyfile.accounts"):
        text = (root / filename).read_text()
        assert "reverse_proxy rent-room-api:8000" in text
        assert text.index("handle /api/rent-room/*") < text.index("reverse_proxy business-api:8000")
    account = (root / "Caddyfile.accounts").read_text()
    assert account.index("handle /api/rent-room/*") < account.index("@account path /api/*")
