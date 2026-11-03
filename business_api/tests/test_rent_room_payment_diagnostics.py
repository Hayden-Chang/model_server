import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.rent_room_main import create_app
from app.rent_room_store import RentRoomStore
from app.rent_room_wechat import SecurePush, WeChatUnavailable, WeChatVirtualPay
from test_rent_room_capacity import FakeWeChat, checkout, login, query, settings


def test_native_failure_is_saved_once_with_order_identity_and_redacted_context(settings, tmp_path):
    path = str(tmp_path / "capacity.sqlite3")
    ledger = RentRoomStore(path)
    provider = FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        event = {"eventId": "diagnostic-event-001", "stage": "native_fail", "errCode": -1,
                 "errMsg": "requestVirtualPayment:fail token=secret-token openid=wx-A https://example.test/?signature=secret-signature",
                 "durationMs": 6200, "platform": "ios", "model": "iPhone 15", "system": "iOS 18.6",
                 "wechatVersion": "8.0.68", "sdkVersion": "3.10.2", "appVersion": "1.0.0"}
        endpoint = "/api/rent-room/orders/request/request-0001/diagnostics"
        for _ in range(2):
            response = client.post(endpoint, headers=headers, json=event)
            assert response.status_code == 200
            assert response.json() == {"receipt": event["eventId"]}
        assert client.post(endpoint, headers=login(client, "B"), json=event).status_code == 404
        assert query(client, headers).json()["status"] == "unpaid"
        assert ledger.packs(ledger.by_id(order["orderId"])["account"]) == 0
    reopened = RentRoomStore(path)
    rows = reopened.db.execute("SELECT order_id,detail FROM rr_payment_events").fetchall()
    assert len(rows) == 1 and rows[0]["order_id"] == order["orderId"]
    data = json.loads(rows[0]["detail"])
    assert data["errCode"] == -1 and data["model"] == "iPhone 15"
    assert data["durationMs"] == 6200
    for secret in ("secret-token", "wx-A", "example.test", "secret-signature"):
        assert secret not in rows[0]["detail"]
    reopened.close()


def test_upstream_200_business_error_logs_order_request_code_and_safe_message(settings, caplog):
    def transport(request):
        return httpx.Response(200, json={"errcode": 268490002,
            "errmsg": "invalid order_id access_token=private-value openid=wx-A"})

    async def run():
        provider = WeChatVirtualPay(settings, httpx.MockTransport(transport))
        provider.access_token, provider.token_expires = "private-token", time.time() + 600
        try:
            with pytest.raises(WeChatUnavailable):
                await provider.query({"id": "a" * 32, "request_id": "request-0001", "openid": "wx-A"})
        finally:
            await provider.close()
    with caplog.at_level(logging.INFO, logger="rent_room.payment"):
        asyncio.run(run())
    assert "request-0001" in caplog.text and "a" * 32 in caplog.text
    assert "268490002" in caplog.text and "invalid order_id" in caplog.text
    assert "private-value" not in caplog.text and "wx-A" not in caplog.text
    assert '"httpStatus":200' in caplog.text


def test_diagnostic_endpoint_validates_payload_limits_and_never_changes_payment(settings):
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        headers = login(client)
        order = checkout(client, headers).json()
        endpoint = "/api/rent-room/orders/request/request-0001/diagnostics"
        event = {"eventId": "diagnostic-event-001", "stage": "native_success"}
        assert client.post(endpoint, json=event).status_code == 401
        for extra in ({"signData": "secret"}, {"errMsg": "x" * 1025}, {"errCode": True}, {"stage": "paid"}):
            assert client.post(endpoint, headers=headers, json={**event, **extra}).status_code == 422
        for n in range(60):
            assert client.post(endpoint, headers=headers, json={**event, "eventId": f"event-{n:08d}"}).status_code == 200
        assert client.post(endpoint, headers=headers, json=event).status_code == 429
        assert ledger.by_id(order["orderId"])["state"] == "unpaid"
        ledger.db.execute("UPDATE rr_payment_events SET created=?", (int(time.time())-31*86400,))
        ledger.db.commit()
        assert client.post(endpoint, headers=headers, json=event).status_code == 200
        assert ledger.db.execute("SELECT count(*) FROM rr_payment_events").fetchone()[0] == 1


def test_notifications_and_reconciliation_are_correlated_without_recording_payload(settings, caplog):
    ledger, provider, push = RentRoomStore(":memory:"), FakeWeChat(settings), SecurePush(settings)
    with caplog.at_level(logging.INFO, logger="rent_room.payment"):
        with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
            headers = login(client)
            order = checkout(client, headers).json()
            provider.remote[order["orderId"]] = {"status": 2}
            event = {"Event": "xpay_goods_deliver_notify", "OpenId": "wx-A", "Env": 0, "OutTradeNo": order["orderId"]}
            encrypted = push.encrypt(json.dumps(event).encode())
            params = {"timestamp": encrypted["TimeStamp"], "nonce": encrypted["Nonce"], "msg_signature": encrypted["MsgSignature"]}
            assert client.post("/api/rent-room/wechat/notify", params=params, json={"Encrypt": encrypted["Encrypt"]}).status_code == 200
            assert query(client, headers).json()["status"] == "paid"
    records = [json.loads(r.message) for r in caplog.records if r.name == "rent_room.payment"]
    for stage in ("checkout", "query_result", "reconciled", "notify_received", "notify_accepted"):
        assert any(r["stage"] == stage and r["orderId"] == order["orderId"] and r["requestId"] == "request-0001" for r in records)
    for secret in ("wx-A", encrypted["Encrypt"], encrypted["MsgSignature"], headers["Authorization"]):
        assert secret not in caplog.text
