import json
import sqlite3

import httpx
import pytest

from app import reverse_ai_quota_export as reverse
from app.quota_store import QuotaStore


def legacy_database(tmp_path) -> str:
    path = tmp_path / "usage.sqlite3"
    store = QuotaStore(str(path), 50)
    store.close()
    return str(path)


def snapshot(*principals):
    return {"exportedAt": "2026-09-09T00:00:00.000Z", "principals": list(principals)}


def principal(name, code, *, used=0, dev=False, completed=(), buckets=None):
    return {
        "principal": name,
        "supportCode": code,
        "developmentEnabled": dev,
        "buckets": [{"period": "free", "used": used, "limit": 50}] if buckets is None else list(buckets),
        "completedRequests": list(completed),
    }


def test_write_snapshot_is_idempotent_and_preserves_usage(tmp_path):
    connection = sqlite3.connect(legacy_database(tmp_path))
    connection.row_factory = sqlite3.Row
    data = snapshot(
        principal("guest_" + "a" * 24, "TF-AAAA-AAAA", used=7, dev=True, completed=["r1", "r2"]),
        principal("guest_" + "b" * 24, "TF-BBBB-BBBB", used=3, buckets=[
            {"period": "free", "used": 3, "limit": 50},
            {"period": "member:2026-09-09", "used": 2, "limit": 50},
        ]),
    )

    first = reverse.write_snapshot(connection, data, now="2026-09-09T01:00:00.000Z")
    second = reverse.write_snapshot(connection, data, now="2026-09-09T02:00:00.000Z")

    assert first == {"principals": 2, "createdBuckets": 3, "updatedBuckets": 0, "completedRequests": 2}
    assert second == {"principals": 2, "createdBuckets": 0, "updatedBuckets": 3, "completedRequests": 0}
    assert connection.execute("select count(*) from quota_principals").fetchone()[0] == 2
    assert connection.execute("select count(*) from quota_buckets").fetchone()[0] == 3
    assert connection.execute("select count(*) from quota_requests").fetchone()[0] == 2
    assert connection.execute(
        "select enabled from development_memberships where principal=?", ("guest_" + "a" * 24,)
    ).fetchone()[0] == 1
    assert connection.execute(
        "select used_count from quota_buckets where principal=? and period_key='free'",
        ("guest_" + "a" * 24,),
    ).fetchone()[0] == 7


def test_write_reactivates_existing_bucket_without_duplicating_rows(tmp_path):
    path = legacy_database(tmp_path)
    principal_id = "guest_" + "c" * 24
    store = QuotaStore(path, 50)
    store.consume(store.reserve(principal_id, "legacy-request"))
    store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    reverse.write_snapshot(
        connection,
        snapshot(principal(principal_id, "TF-CCCC-CCCC", used=9, completed=["legacy-request"])),
    )

    buckets = connection.execute(
        "select used_count, active, deactivated_at from quota_buckets where principal=?", (principal_id,)
    ).fetchall()
    assert len(buckets) == 1
    assert buckets[0]["used_count"] == 9
    assert buckets[0]["active"] == 1
    assert buckets[0]["deactivated_at"] is None
    assert connection.execute("select count(*) from quota_requests").fetchone()[0] == 1


def test_write_deactivates_periods_missing_from_the_export(tmp_path):
    path = legacy_database(tmp_path)
    principal_id = "guest_" + "d" * 24
    store = QuotaStore(path, 50, development_principals=frozenset({principal_id}))
    store.set_membership(principal_id, True)
    store.consume(store.reserve(principal_id, "member-request"))
    store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    reverse.write_snapshot(connection, snapshot(principal(principal_id, "TF-DDDD-DDDD", used=4)))

    member = connection.execute(
        "select active, deactivated_at from quota_buckets where principal=? and period_key like 'member:%'",
        (principal_id,),
    ).fetchone()
    assert member["active"] == 0
    assert member["deactivated_at"] is not None


def test_write_rejects_incomplete_legacy_schema():
    connection = sqlite3.connect(":memory:")
    with pytest.raises(RuntimeError, match="incomplete"):
        reverse.write_snapshot(connection, snapshot())


def test_rpc_calls_use_service_role_key_and_expected_endpoints():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("ai_quota_export_legacy"):
            return httpx.Response(200, json=snapshot())
        return httpx.Response(200, json={"gateOpen": False, "refundedReservations": 2})

    transport = httpx.MockTransport(handler)
    assert reverse.fetch_snapshot("https://project.supabase.co", "service-key", transport) == snapshot()
    result = reverse.close_new_authority(
        "https://project.supabase.co", "service-key", reset_import=True, transport=transport
    )

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "ai_quota_export_legacy", "ai_quota_rollback", "ai_quota_reset_import"
    ]
    for request in requests:
        assert request.headers["apikey"] == "service-key"
        assert request.headers["authorization"] == "Bearer service-key"
        assert json.loads(request.content) == {}
    assert result["rollback"]["gateOpen"] is False


def test_main_dry_run_fetches_but_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(reverse, "fetch_snapshot", lambda url, key: snapshot(
        principal("guest_" + "e" * 24, "TF-EEEE-EEEE", used=2)
    ))
    missing = tmp_path / "not-created.sqlite3"

    assert reverse.main(["--database", str(missing), "--dry-run"]) == 0

    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)["dryRun"] is True


def test_main_exports_then_closes_and_resets(tmp_path, monkeypatch, capsys):
    path = legacy_database(tmp_path)
    principal_id = "guest_" + "f" * 24
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(reverse, "fetch_snapshot", lambda url, key: snapshot(
        principal(principal_id, "TF-FFFF-FFFF", used=5, completed=["request-1"])
    ))
    resets = []
    monkeypatch.setattr(
        reverse, "close_new_authority",
        lambda url, key, *, reset_import, transport=None: resets.append(reset_import) or {
            "rollback": {"gateOpen": False}, "resetImport": {"removedGuestPrincipals": 1}
        },
    )

    assert reverse.main(["--database", path, "--reset-import"]) == 0

    assert resets == [True]
    assert json.loads(capsys.readouterr().out)["written"]["principals"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select used_count from quota_buckets where principal=?", (principal_id,)
        ).fetchone()[0] == 5


def test_main_requires_credentials(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert reverse.main([]) == 2
    assert "SUPABASE_URL" in capsys.readouterr().err


def test_main_failure_does_not_print_unrelated_response_body(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

    def fail(url, key):
        request = httpx.Request("POST", url + "/rest/v1/rpc/ai_quota_export_legacy")
        response = httpx.Response(
            404, request=request, json={"message": "function missing", "detail": "do-not-print"}
        )
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(reverse, "fetch_snapshot", fail)

    assert reverse.main([]) == 1
    error = capsys.readouterr().err
    assert "HTTP 404" in error and "function missing" in error
    assert "do-not-print" not in error
