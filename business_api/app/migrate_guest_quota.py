"""Import a stopped legacy service's quota metadata; never copy model content."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .account_backend import AccountAPISettings, AccountBackend, support_code


def snapshots(path: Path, default_limit: int) -> list[dict]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("begin")
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        if connection.execute("select 1 from quota_requests where state='reserved' and updated_at>=? limit 1", (cutoff,)).fetchone():
            raise RuntimeError("Legacy requests still in flight; stop the service and let reservations expire before importing")
        principals = {row["principal"]: dict(row) for row in connection.execute("select * from quota_principals")}
        memberships = dict(connection.execute("select principal,enabled from development_memberships"))
        result = []
        for principal in sorted(principals.keys() | memberships.keys()):
            p = principals.get(principal, {"support_code": support_code(principal)})
            buckets = list(connection.execute("select * from quota_buckets where principal=? and active=1", (principal,)))
            quota_limit = next((b["quota_limit"] for b in buckets if b["period_key"] == "free"), default_limit)
            counts = []
            for b in buckets:
                pending = connection.execute("select count(*) from quota_requests where bucket_id=? and state='reserved'", (b["id"],)).fetchone()[0]
                counts.append({"period": b["period_key"], "used": max(0, b["used_count"] - pending)})
            completed = [row[0] for row in connection.execute(
                "select distinct r.request_id from quota_requests r join quota_buckets b on b.id=r.bucket_id "
                "where b.principal=? and r.state='consumed' order by r.request_id", (principal,))]
            snapshot = {"principal": principal, "supportCode": p["support_code"], "limit": quota_limit,
                        "developmentEnabled": bool(memberships.get(principal, False)),
                        "buckets": counts, "completedRequests": completed}
            snapshot["importHash"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            result.append(snapshot)
        return result
    finally:
        connection.close()


async def migrate(path: Path):
    settings = AccountAPISettings()
    data = snapshots(path, settings.time_fragment_guest_quota_limit)
    backend = AccountBackend(settings)
    try:
        for snapshot in data:
            await backend.quota("import", **snapshot)
        await backend.quota("finish_import")
        print(json.dumps({"importedPrincipals": len(data), "ready": True}))
    finally:
        await backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--legacy-service-stopped", action="store_true", required=True)
    arguments = parser.parse_args()
    asyncio.run(migrate(arguments.database))
