import base64
import hashlib
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class QuotaReservation:
    bucket_id: int
    request_id: str
    reservation_token: str
    support_code: str
    quota_limit: int
    used: int
    remaining: int


@dataclass(frozen=True)
class QuotaStatus:
    support_code: str
    quota_limit: int
    used: int
    remaining: int


class QuotaExceeded(Exception):
    def __init__(self, quota_status: QuotaStatus) -> None:
        super().__init__("AI quota exhausted")
        self.quota_status = quota_status


class DuplicateRequestInProgress(Exception):
    pass


class DuplicateRequestCompleted(Exception):
    pass


class QuotaStore:
    def __init__(self, database_path: str, default_limit: int) -> None:
        if database_path != ":memory:":
            database_file = Path(database_path).expanduser()
            database_file.parent.mkdir(parents=True, exist_ok=True)
            database_file.touch(mode=0o600, exist_ok=True)
            database_file.chmod(0o600)
        self._default_limit = default_limit
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if database_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS quota_principals (
                    principal TEXT PRIMARY KEY,
                    support_code TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_buckets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    principal TEXT NOT NULL REFERENCES quota_principals(principal),
                    quota_limit INTEGER NOT NULL CHECK (quota_limit > 0),
                    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    deactivated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS quota_requests (
                    bucket_id INTEGER NOT NULL REFERENCES quota_buckets(id),
                    request_id TEXT NOT NULL,
                    reservation_token TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'consumed', 'refunded')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (bucket_id, request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_quota_active_principal
                    ON quota_buckets(principal) WHERE active = 1;
                CREATE INDEX IF NOT EXISTS idx_quota_support_code
                    ON quota_principals(support_code);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def reserve(self, principal: str, request_id: str) -> QuotaReservation:
        now = _utc_now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                support_code = self._support_code_for_principal(principal, now)
                bucket = self._active_bucket(principal)
                if bucket is None:
                    cursor = self._connection.execute(
                        """INSERT INTO quota_buckets (
                            principal, quota_limit, used_count, active, created_at
                        ) VALUES (?, ?, 0, 1, ?)""",
                        (principal, self._default_limit, now),
                    )
                    bucket_id = int(cursor.lastrowid)
                    quota_limit = self._default_limit
                    used = 0
                else:
                    bucket_id = int(bucket["id"])
                    quota_limit = int(bucket["quota_limit"])
                    used = int(bucket["used_count"])

                expired = self._connection.execute(
                    """UPDATE quota_requests SET state = 'refunded', updated_at = ?
                    WHERE bucket_id = ? AND state = 'reserved' AND updated_at < ?""",
                    (now, bucket_id, _utc_before(timedelta(minutes=10))),
                )
                if expired.rowcount:
                    used = max(0, used - int(expired.rowcount))
                    self._connection.execute(
                        "UPDATE quota_buckets SET used_count = ? WHERE id = ?",
                        (used, bucket_id),
                    )

                existing = self._connection.execute(
                    "SELECT state FROM quota_requests WHERE bucket_id = ? AND request_id = ?",
                    (bucket_id, request_id),
                ).fetchone()
                if existing is not None and existing["state"] == "reserved":
                    raise DuplicateRequestInProgress
                if existing is not None and existing["state"] == "consumed":
                    raise DuplicateRequestCompleted
                if used >= quota_limit:
                    raise QuotaExceeded(
                        QuotaStatus(
                            support_code=support_code,
                            quota_limit=quota_limit,
                            used=used,
                            remaining=0,
                        )
                    )

                reservation_token = secrets.token_urlsafe(18)
                if existing is None:
                    self._connection.execute(
                        """INSERT INTO quota_requests (
                            bucket_id, request_id, reservation_token, state, created_at, updated_at
                        ) VALUES (?, ?, ?, 'reserved', ?, ?)""",
                        (bucket_id, request_id, reservation_token, now, now),
                    )
                else:
                    self._connection.execute(
                        """UPDATE quota_requests
                        SET reservation_token = ?, state = 'reserved', updated_at = ?
                        WHERE bucket_id = ? AND request_id = ?""",
                        (reservation_token, now, bucket_id, request_id),
                    )
                used += 1
                self._connection.execute(
                    "UPDATE quota_buckets SET used_count = ? WHERE id = ?",
                    (used, bucket_id),
                )
                self._connection.commit()
                return QuotaReservation(
                    bucket_id=bucket_id,
                    request_id=request_id,
                    reservation_token=reservation_token,
                    support_code=support_code,
                    quota_limit=quota_limit,
                    used=used,
                    remaining=max(0, quota_limit - used),
                )
            except Exception:
                self._connection.rollback()
                raise

    def consume(self, reservation: QuotaReservation) -> None:
        self._finish(reservation, "consumed", decrement=False)

    def refund(self, reservation: QuotaReservation) -> None:
        self._finish(reservation, "refunded", decrement=True)

    def status(self, support_code: str) -> QuotaStatus | None:
        with self._lock:
            principal = self._connection.execute(
                "SELECT principal FROM quota_principals WHERE support_code = ?",
                (support_code.upper(),),
            ).fetchone()
            if principal is None:
                return None
            bucket = self._active_bucket(principal["principal"])
            if bucket is None:
                return QuotaStatus(support_code.upper(), self._default_limit, 0, self._default_limit)
            quota_limit = int(bucket["quota_limit"])
            used = int(bucket["used_count"])
            return QuotaStatus(
                support_code.upper(),
                quota_limit,
                used,
                max(0, quota_limit - used),
            )

    def reset(self, support_code: str) -> QuotaStatus | None:
        now = _utc_now()
        normalized = support_code.upper()
        with self._lock, self._connection:
            principal = self._connection.execute(
                "SELECT principal FROM quota_principals WHERE support_code = ?",
                (normalized,),
            ).fetchone()
            if principal is None:
                return None
            self._connection.execute(
                """UPDATE quota_buckets SET active = 0, deactivated_at = ?
                WHERE principal = ? AND active = 1""",
                (now, principal["principal"]),
            )
            self._connection.execute(
                """INSERT INTO quota_buckets (
                    principal, quota_limit, used_count, active, created_at
                ) VALUES (?, ?, 0, 1, ?)""",
                (principal["principal"], self._default_limit, now),
            )
        return QuotaStatus(normalized, self._default_limit, 0, self._default_limit)

    def reset_all(self) -> int:
        now = _utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE quota_buckets SET active = 0, deactivated_at = ?
                WHERE active = 1""",
                (now,),
            )
            return int(cursor.rowcount)

    def _finish(
        self,
        reservation: QuotaReservation,
        state: str,
        *,
        decrement: bool,
    ) -> None:
        now = _utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE quota_requests SET state = ?, updated_at = ?
                WHERE bucket_id = ? AND request_id = ?
                    AND reservation_token = ? AND state = 'reserved'""",
                (
                    state,
                    now,
                    reservation.bucket_id,
                    reservation.request_id,
                    reservation.reservation_token,
                ),
            )
            if decrement and cursor.rowcount:
                self._connection.execute(
                    """UPDATE quota_buckets SET used_count = MAX(used_count - 1, 0)
                    WHERE id = ?""",
                    (reservation.bucket_id,),
                )

    def _active_bucket(self, principal: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """SELECT id, quota_limit, used_count FROM quota_buckets
            WHERE principal = ? AND active = 1""",
            (principal,),
        ).fetchone()

    def _support_code_for_principal(self, principal: str, now: str) -> str:
        existing = self._connection.execute(
            "SELECT support_code FROM quota_principals WHERE principal = ?",
            (principal,),
        ).fetchone()
        if existing is not None:
            self._connection.execute(
                "UPDATE quota_principals SET last_seen_at = ? WHERE principal = ?",
                (now, principal),
            )
            return str(existing["support_code"])

        for attempt in range(256):
            support_code = _candidate_support_code(principal, attempt)
            collision = self._connection.execute(
                "SELECT principal FROM quota_principals WHERE support_code = ?",
                (support_code,),
            ).fetchone()
            if collision is not None:
                continue
            self._connection.execute(
                """INSERT INTO quota_principals (
                    principal, support_code, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?)""",
                (principal, support_code, now, now),
            )
            return support_code
        raise RuntimeError("unable to allocate a unique support code")


def _candidate_support_code(principal: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{attempt}:{principal}".encode("utf-8")).digest()
    encoded = base64.b32encode(digest[:5]).decode("ascii")
    return f"TF-{encoded[:4]}-{encoded[4:]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_before(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat().replace("+00:00", "Z")
