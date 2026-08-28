import sqlite3
from pathlib import Path

import pytest

from app.quota_store import (
    DuplicateRequestCompleted,
    DuplicateRequestInProgress,
    QuotaExceeded,
    QuotaStore,
)


def test_default_beta_quota_allows_fifty_usable_requests_then_rejects_next() -> None:
    store = QuotaStore(":memory:", default_limit=50)
    try:
        for index in range(50):
            reservation = store.reserve("guest_one", f"request-{index + 1}")
            store.consume(reservation)

        assert reservation.used == 50
        assert reservation.remaining == 0
        with pytest.raises(QuotaExceeded):
            store.reserve("guest_one", "request-51")
    finally:
        store.close()


def test_quota_store_enforces_limit_and_does_not_double_charge_request_id() -> None:
    store = QuotaStore(":memory:", default_limit=2)
    try:
        first = store.reserve("guest_one", "request-1")
        store.consume(first)
        with pytest.raises(DuplicateRequestCompleted):
            store.reserve("guest_one", "request-1")
        second = store.reserve("guest_one", "request-2")
        store.consume(second)

        with pytest.raises(QuotaExceeded) as exhausted:
            store.reserve("guest_one", "request-3")

        assert first.support_code == second.support_code
        assert exhausted.value.quota_status.used == 2
        assert exhausted.value.quota_status.remaining == 0
    finally:
        store.close()


def test_quota_store_refunds_failures_and_rejects_concurrent_duplicate() -> None:
    store = QuotaStore(":memory:", default_limit=1)
    try:
        failed = store.reserve("guest_one", "request-1")
        with pytest.raises(DuplicateRequestInProgress):
            store.reserve("guest_one", "request-1")

        store.refund(failed)
        retried = store.reserve("guest_one", "request-1")
        store.consume(retried)

        status = store.status(retried.support_code)
        assert status is not None
        assert status.used == 1
        assert status.remaining == 0
    finally:
        store.close()


def test_quota_store_resets_one_or_all_installations() -> None:
    store = QuotaStore(":memory:", default_limit=1)
    try:
        first = store.reserve("guest_one", "request-1")
        store.consume(first)
        second = store.reserve("guest_two", "request-1")
        store.consume(second)

        reset = store.reset(first.support_code)
        assert reset is not None
        assert reset.used == 0
        assert reset.remaining == 1
        store.consume(store.reserve("guest_one", "request-2"))

        assert store.reset_all() == 2
        assert store.status(first.support_code).remaining == 1  # type: ignore[union-attr]
        assert store.status(second.support_code).remaining == 1  # type: ignore[union-attr]
    finally:
        store.close()


def test_support_code_is_stable_opaque_and_unknown_codes_do_not_reset() -> None:
    store = QuotaStore(":memory:", default_limit=1)
    try:
        reservation = store.reserve("guest_private_device_identity", "request-1")
        store.refund(reservation)
        repeated = store.reserve("guest_private_device_identity", "request-2")

        assert repeated.support_code == reservation.support_code
        assert repeated.support_code.startswith("TF-")
        assert "PRIVATE" not in repeated.support_code
        assert store.reset("TF-AAAA-AAAA") is None
    finally:
        store.close()


def test_stale_reservation_is_recovered_after_process_restart(tmp_path: Path) -> None:
    database_path = str(tmp_path / "quota.sqlite3")
    first_store = QuotaStore(database_path, default_limit=1)
    first_store.reserve("guest_one", "abandoned-request")
    first_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE quota_requests SET updated_at = '2000-01-01T00:00:00Z'"
        )

    restarted_store = QuotaStore(database_path, default_limit=1)
    try:
        recovered = restarted_store.reserve("guest_one", "new-request")

        assert recovered.used == 1
        assert recovered.remaining == 0
    finally:
        restarted_store.close()
