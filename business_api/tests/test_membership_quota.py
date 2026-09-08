from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.guest_auth import GuestTokenCodec
from app.quota_store import QuotaStore, QuotaExceeded, DuplicateRequestCompleted
from test_time_fragment_api import FakeModelClient, guest_headers, operations_output, request_payload, settings


def test_daily_member_limit_preserves_free_pool_and_toggle_does_not_refill() -> None:
    store = QuotaStore(":memory:", 50, development_principals=frozenset({"guest_one"}))
    try:
        store.consume(store.reserve("guest_one", "free-1"))
        store.set_membership("guest_one", True)
        for index in range(50):
            store.consume(store.reserve("guest_one", f"member-{index}"))
        with pytest.raises(QuotaExceeded) as exhausted:
            store.reserve("guest_one", "over-limit")
        assert exhausted.value.quota_status.resets_at is not None
        store.set_membership("guest_one", False)
        assert store.membership_status("guest_one")["remaining"] == 49
        store.set_membership("guest_one", True)
        assert store.membership_status("guest_one")["remaining"] == 0
    finally:
        store.close()


def test_member_refills_at_server_midnight_not_client_time() -> None:
    now = [datetime(2026, 9, 8, 15, 59, tzinfo=timezone.utc)]
    store = QuotaStore(":memory:", 50, development_principals=frozenset({"guest_one"}), clock=lambda: now[0])
    try:
        store.set_membership("guest_one", True)
        store.consume(store.reserve("guest_one", "day-1"))
        assert store.membership_status("guest_one")["remaining"] == 49
        assert store.membership_status("guest_one")["resetsAt"] == "2026-09-09T00:00:00+08:00"
        now[0] += timedelta(minutes=2)
        assert store.membership_status("guest_one")["remaining"] == 50
        store.consume(store.reserve("guest_one", "day-2"))
        with pytest.raises(DuplicateRequestCompleted):
            store.reserve("guest_one", "day-1")
        assert store.membership_status("guest_one")["remaining"] == 49
    finally:
        store.close()


def test_member_refund_and_cross_mode_duplicate_do_not_double_charge() -> None:
    store = QuotaStore(":memory:", 50, development_principals=frozenset({"guest_one"}))
    try:
        store.consume(store.reserve("guest_one", "completed"))
        store.set_membership("guest_one", True)
        with pytest.raises(DuplicateRequestCompleted):
            store.reserve("guest_one", "completed")
        pending = store.reserve("guest_one", "retry")
        store.refund(pending)
        store.refund(pending)
        assert store.membership_status("guest_one")["remaining"] == 50
        store.consume(store.reserve("guest_one", "retry"))
        assert store.membership_status("guest_one")["remaining"] == 49
    finally:
        store.close()


def test_member_persistence_and_allowlist_revocation(tmp_path: Path) -> None:
    database = str(tmp_path / "membership.sqlite3")
    store = QuotaStore(database, 50, development_principals=frozenset({"guest_one"}))
    store.set_membership("guest_one", True)
    store.consume(store.reserve("guest_one", "used"))
    store.close()
    restored = QuotaStore(database, 50, development_principals=frozenset({"guest_one"}))
    assert restored.membership_status("guest_one")["remaining"] == 49
    restored.close()
    revoked = QuotaStore(database, 50)
    try:
        assert revoked.membership_status("guest_one")["enabled"] is False
        assert revoked.membership_status("guest_one")["remaining"] == 50
        with pytest.raises(PermissionError):
            revoked.set_membership("guest_one", True)
    finally:
        revoked.close()


def test_legacy_bucket_migration_preserves_consumption(tmp_path: Path) -> None:
    database = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.executescript("""
          CREATE TABLE quota_principals(principal TEXT PRIMARY KEY, support_code TEXT UNIQUE, created_at TEXT, last_seen_at TEXT);
          CREATE TABLE quota_buckets(id INTEGER PRIMARY KEY, principal TEXT, quota_limit INTEGER, used_count INTEGER, active INTEGER, created_at TEXT, deactivated_at TEXT);
          CREATE UNIQUE INDEX idx_quota_active_principal ON quota_buckets(principal) WHERE active = 1;
          INSERT INTO quota_principals VALUES ('guest_one', 'TF-AAAA-AAAA', 'old', 'old');
          INSERT INTO quota_buckets VALUES (1, 'guest_one', 50, 13, 1, 'old', NULL);
        """)
    store = QuotaStore(database, 50, development_principals=frozenset({"guest_one"}))
    try:
        assert store.status("TF-AAAA-AAAA").remaining == 37
        store.set_membership("guest_one", True)
        store.consume(store.reserve("guest_one", "member"))
        store.set_membership("guest_one", False)
        assert store.status("TF-AAAA-AAAA").remaining == 37
    finally:
        store.close()


def test_development_endpoint_requires_authenticated_allowlisted_installation(settings) -> None:
    allowed = settings.model_copy(update={"time_fragment_development_device_ids": "time-fragment-allowed-device"})
    with TestClient(create_app(allowed, FakeModelClient([]))) as client:
        assert client.post("/api/development/membership", json={"enabled": True}).status_code == 401
        denied = guest_headers(client, "time-fragment-another-device")
        assert client.post("/api/development/membership", headers=denied, json={"enabled": True}).status_code == 403
        headers = guest_headers(client, "time-fragment-allowed-device")
        assert client.post("/api/development/membership", headers=headers, json={"enabled": "true"}).status_code == 422
        assert client.post("/api/development/membership", headers=headers, json={"enabled": True, "limit": 500}).status_code == 422
        enabled = client.post("/api/development/membership", headers=headers, json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["remaining"] == 50
        assert client.get("/api/development/membership", headers=headers).json()["enabled"] is True


def test_actual_plan_route_uses_member_bucket_and_returns_daily_reset(settings) -> None:
    device = "time-fragment-allowed-device"
    principal = GuestTokenCodec.device_key(device)
    configured = settings.model_copy(update={"time_fragment_development_device_ids": device})
    quotas = QuotaStore(":memory:", 50, development_principals=frozenset({principal}))
    fake = FakeModelClient([operations_output([]), operations_output([])])
    with TestClient(create_app(configured, fake, quota_store=quotas)) as client:
        headers = guest_headers(client, device)
        assert client.post("/api/development/membership", headers=headers, json={"enabled": True}).status_code == 200
        first = client.post("/api/plan/parse", headers=headers, json=request_payload(request_id="actual-member"))
        assert first.status_code == 200
        assert client.get("/api/development/membership", headers=headers).json()["remaining"] == 49
        for index in range(49):
            quotas.consume(quotas.reserve(principal, f"fill-{index}"))
        exhausted = client.post("/api/plan/parse", headers=headers, json=request_payload(request_id="blocked"))
        assert exhausted.status_code == 429
        assert exhausted.json()["detail"]["code"] == "AI_DAILY_QUOTA_EXHAUSTED"
        assert exhausted.json()["detail"]["resetsAt"]
        assert client.post("/api/development/membership", headers=headers, json={"enabled": False}).json()["remaining"] == 50
        free = client.post("/api/plan/parse", headers=headers, json=request_payload(request_id="actual-free"))
        assert free.status_code == 200
        assert len(fake.calls) == 2


def test_reservation_uses_one_server_instant_across_midnight() -> None:
    before = datetime(2026, 9, 8, 15, 59, 59, tzinfo=timezone.utc)
    moments = iter([before, before + timedelta(seconds=2)])
    store = QuotaStore(":memory:", 50, development_principals=frozenset({"guest_one"}), clock=lambda: next(moments))
    try:
        store.set_membership("guest_one", True)
        reservation = store.reserve("guest_one", "boundary")
        assert reservation.remaining == 49
        assert store.membership_status("guest_one")["remaining"] == 50
    finally:
        store.close()
