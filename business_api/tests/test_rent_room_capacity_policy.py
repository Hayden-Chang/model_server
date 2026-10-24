import pytest
from fastapi.testclient import TestClient

from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore
from test_rent_room_capacity import FakeWeChat, settings, checkout, query


@pytest.mark.parametrize("free", [0, 1, 4, 20])
def test_v2_configured_policy_and_legacy_contracts(settings, free):
    settings = settings.model_copy(update={"free_capacity": free})
    ledger, provider = RentRoomStore(":memory:"), FakeWeChat(settings)
    with TestClient(create_app(settings, provider=provider, store=ledger)) as client:
        for version, expected in [(None, 2), ("1", 1), ("unknown", 2), ("2", free)]:
            client.headers.pop("x-rent-room-capacity-version", None)
            if version:
                client.headers["x-rent-room-capacity-version"] = version
            catalog = client.get("/api/rent-room/catalog").json()
            response = client.post("/api/rent-room/session", json={"code": "A"}).json()
            assert response["limit"] == expected
            if version == "2":
                assert response["capacity"] == catalog["capacity"] == {"version": 2, "freeLimit": free}
            else:
                assert "capacity" not in response and "capacity" not in catalog
        headers = {"Authorization": "Bearer " + response["token"]}
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {
            "packs": 0, "limit": free, "capacity": {"version": 2, "freeLimit": free}}
        order = checkout(client, headers).json()
        assert order["limit"] == free and order["capacity"]["freeLimit"] == free
        provider.remote[order["orderId"]] = {"status": 2}
        paid = query(client, headers).json()
        assert paid["packs"] == 1 and paid["limit"] == free + 10
        assert paid["capacity"]["freeLimit"] == free
        client.headers["x-rent-room-capacity-version"] = "1"
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 1, "limit": 11}
        del client.headers["x-rent-room-capacity-version"]
        assert client.get("/api/rent-room/entitlements", headers=headers).json() == {"packs": 1, "limit": 12}


def test_policy_is_public_even_when_payments_and_login_are_disabled(monkeypatch):
    monkeypatch.setenv("RENT_ROOM_FREE_CAPACITY", "6")
    with TestClient(create_app(RentRoomSettings(_env_file=None))) as client:
        response = client.get("/api/rent-room/catalog", headers={"x-rent-room-capacity-version": "2"})
        assert response.status_code == 200
        assert response.json()["available"] is False
        assert response.json()["capacity"] == {"version": 2, "freeLimit": 6}


@pytest.mark.parametrize("value", [-1, 1.5, 9007199254740992])
def test_invalid_free_capacity_is_rejected(value):
    with pytest.raises(ValueError):
        RentRoomSettings(_env_file=None, free_capacity=value)
