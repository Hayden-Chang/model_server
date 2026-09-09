"""Reverse-export the Postgres AI ledger into the legacy SQLite store.

This runs only during an emergency rollback from the account-aware API to the
legacy single-service deployment. ``scripts/rollback-account-ai-cutover.sh``
invokes it inside the ``time-fragment-api`` image, which already has the Supabase
credentials and the legacy volume mounted read-only.

The export is written transactionally and is safe to repeat. It never deletes
Postgres rows; ``ai_quota_reset_import`` does that only after a successful write
and only while the new ledger gate is closed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

DATABASE_DEFAULT = "/var/lib/model-server/usage.sqlite3"
REQUIRED_TABLES = {"quota_principals", "quota_buckets", "quota_requests", "development_memberships"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rpc(url: str, key: str, name: str, transport: httpx.BaseTransport | None = None) -> Any:
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.post(
            url.rstrip("/") + "/rest/v1/rpc/" + name,
            headers={"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={},
        )
    response.raise_for_status()
    return response.json()


def fetch_snapshot(url: str, key: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    snapshot = _rpc(url, key, "ai_quota_export_legacy", transport)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("principals"), list):
        raise RuntimeError("unexpected legacy export response")
    return snapshot


def close_new_authority(
    url: str,
    key: str,
    *,
    reset_import: bool,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    result = {"rollback": _rpc(url, key, "ai_quota_rollback", transport)}
    if reset_import:
        result["resetImport"] = _rpc(url, key, "ai_quota_reset_import", transport)
    return result


def summarize(snapshot: dict[str, Any]) -> dict[str, int]:
    principals = snapshot["principals"]
    return {
        "principals": len(principals),
        "buckets": sum(len(principal.get("buckets") or []) for principal in principals),
        "completedRequests": sum(len(principal.get("completedRequests") or []) for principal in principals),
        "developmentMemberships": sum(1 for principal in principals if principal.get("developmentEnabled")),
    }


def _require_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    missing = REQUIRED_TABLES - tables
    if missing:
        raise RuntimeError("legacy quota schema is incomplete: " + ",".join(sorted(missing)))
    columns = {row[1] for row in connection.execute("pragma table_info(quota_buckets)")}
    if "period_key" not in columns:
        raise RuntimeError("legacy quota schema predates period_key")


def _bucket_row(connection: sqlite3.Connection, principal: str, period: str) -> int | None:
    active = connection.execute(
        "select id from quota_buckets where principal=? and period_key=? and active=1",
        (principal, period),
    ).fetchone()
    if active is not None:
        return int(active[0])
    newest = connection.execute(
        "select id from quota_buckets where principal=? and period_key=? order by id desc limit 1",
        (principal, period),
    ).fetchone()
    return None if newest is None else int(newest[0])


def _write_bucket(
    connection: sqlite3.Connection,
    principal: str,
    bucket: dict[str, Any],
    now: str,
) -> tuple[int, bool]:
    period = str(bucket.get("period") or "free")
    limit = max(1, int(bucket.get("limit") or 50))
    used = max(0, int(bucket.get("used") or 0))
    bucket_id = _bucket_row(connection, principal, period)
    if bucket_id is None:
        cursor = connection.execute(
            "insert into quota_buckets(principal,quota_limit,used_count,active,created_at,period_key)"
            " values(?,?,?,1,?,?)",
            (principal, limit, used, now, period),
        )
        return int(cursor.lastrowid), True
    connection.execute(
        "update quota_buckets set quota_limit=?, used_count=?, active=1, deactivated_at=null where id=?",
        (limit, used, bucket_id),
    )
    return bucket_id, False


def _write_principal(
    connection: sqlite3.Connection,
    principal: dict[str, Any],
    now: str,
) -> dict[str, int]:
    identifier = str(principal["principal"])
    connection.execute(
        "insert into quota_principals(principal,support_code,created_at,last_seen_at) values(?,?,?,?)"
        " on conflict(principal) do update set support_code=excluded.support_code,"
        " last_seen_at=excluded.last_seen_at",
        (identifier, str(principal["supportCode"]), now, now),
    )
    buckets = list(principal.get("buckets") or [])
    periods = [str(bucket.get("period") or "free") for bucket in buckets]
    if periods:
        placeholders = ",".join("?" for _ in periods)
        connection.execute(
            "update quota_buckets set active=0, deactivated_at=?"
            f" where principal=? and active=1 and period_key not in ({placeholders})",
            (now, identifier, *periods),
        )
    else:
        connection.execute(
            "update quota_buckets set active=0, deactivated_at=? where principal=? and active=1",
            (now, identifier),
        )

    bucket_ids: dict[str, int] = {}
    created = 0
    updated = 0
    for bucket in buckets:
        period = str(bucket.get("period") or "free")
        bucket_id, inserted = _write_bucket(connection, identifier, bucket, now)
        bucket_ids[period] = bucket_id
        created += 1 if inserted else 0
        updated += 0 if inserted else 1

    fallback = bucket_ids.get("free") or next(iter(bucket_ids.values()), None)
    completed = 0
    for request_id in principal.get("completedRequests") or []:
        if fallback is None:
            break
        exists = connection.execute(
            "select 1 from quota_requests r join quota_buckets b on b.id=r.bucket_id"
            " where b.principal=? and r.request_id=? limit 1",
            (identifier, str(request_id)),
        ).fetchone()
        if exists is not None:
            continue
        connection.execute(
            "insert into quota_requests(bucket_id,request_id,reservation_token,state,created_at,updated_at)"
            " values(?,?,?,?,?,?)",
            (fallback, str(request_id), secrets.token_urlsafe(18), "consumed", now, now),
        )
        completed += 1

    connection.execute(
        "insert into development_memberships(principal,enabled) values(?,?)"
        " on conflict(principal) do update set enabled=excluded.enabled",
        (identifier, 1 if principal.get("developmentEnabled") else 0),
    )
    return {"createdBuckets": created, "updatedBuckets": updated, "completedRequests": completed}


def write_snapshot(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, int]:
    instant = now or _now()
    _require_schema(connection)
    totals = {"principals": 0, "createdBuckets": 0, "updatedBuckets": 0, "completedRequests": 0}
    connection.execute("begin immediate")
    try:
        for principal in snapshot["principals"]:
            result = _write_principal(connection, principal, instant)
            totals["principals"] += 1
            totals["createdBuckets"] += result["createdBuckets"]
            totals["updatedBuckets"] += result["updatedBuckets"]
            totals["completedRequests"] += result["completedRequests"]
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return totals


def _failure_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        detail = ""
        try:
            body = error.response.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                detail = ": " + body["message"][:200]
        except ValueError:
            pass
        return f"HTTP {error.response.status_code}{detail}"
    return f"{type(error).__name__}: {error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DATABASE_DEFAULT)
    parser.add_argument("--step", choices=("all", "export", "close"), default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-import", action="store_true")
    arguments = parser.parse_args(argv)

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required", file=sys.stderr)
        return 2

    result: dict[str, Any] = {}
    try:
        if arguments.step in ("all", "export"):
            snapshot = fetch_snapshot(url, key)
            result["export"] = summarize(snapshot)
            if arguments.dry_run:
                result["dryRun"] = True
            else:
                connection = sqlite3.connect(arguments.database, timeout=30)
                try:
                    connection.execute("pragma busy_timeout=30000")
                    connection.execute("pragma foreign_keys=ON")
                    result["written"] = write_snapshot(connection, snapshot)
                finally:
                    connection.close()
        if arguments.step in ("all", "close"):
            if arguments.dry_run:
                result["close"] = {"dryRun": True, "resetImport": arguments.reset_import}
            else:
                result["close"] = close_new_authority(url, key, reset_import=arguments.reset_import)
    except (httpx.HTTPError, sqlite3.Error, RuntimeError, KeyError, TypeError, ValueError) as error:
        print("reverse quota export failed: " + _failure_message(error), file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
