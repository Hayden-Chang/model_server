import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .observability import ModelCallCapture, TokenUsage


@dataclass(frozen=True)
class InferenceCapture:
    request_id: str
    device_key: str
    route: str
    pipeline: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    status_code: int
    request_content: Any
    response_content: Any | None
    model_calls: list[ModelCallCapture]
    usage: TokenUsage | None
    usage_complete: bool


class UsageStore:
    def __init__(self, database_path: str, content_retention_days: int) -> None:
        if database_path != ":memory:":
            database_file = Path(database_path).expanduser()
            database_file.parent.mkdir(parents=True, exist_ok=True)
            database_file.touch(mode=0o600, exist_ok=True)
            database_file.chmod(0o600)
        self._content_retention_days = content_retention_days
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
                CREATE TABLE IF NOT EXISTS inference_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    device_key TEXT NOT NULL,
                    route TEXT NOT NULL,
                    pipeline TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    status_code INTEGER NOT NULL,
                    request_content TEXT,
                    response_content TEXT,
                    model_call_count INTEGER NOT NULL CHECK (model_call_count >= 0),
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    usage_complete INTEGER NOT NULL CHECK (usage_complete IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inference_request_id INTEGER NOT NULL REFERENCES inference_requests(id) ON DELETE CASCADE,
                    call_index INTEGER NOT NULL,
                    pipeline TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    input_content TEXT,
                    output_content TEXT,
                    provider_model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    usage_complete INTEGER NOT NULL CHECK (usage_complete IN (0, 1)),
                    error_type TEXT,
                    error_message TEXT,
                    UNIQUE(inference_request_id, call_index)
                );
                CREATE INDEX IF NOT EXISTS idx_inference_device_started
                    ON inference_requests(device_key, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_inference_started
                    ON inference_requests(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_inference_completed
                    ON inference_requests(completed_at);
                CREATE INDEX IF NOT EXISTS idx_model_calls_completed
                    ON model_calls(completed_at);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record(self, capture: InferenceCapture) -> None:
        usage = capture.usage
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO inference_requests (
                    request_id, device_key, route, pipeline, started_at, completed_at,
                    duration_ms, status_code, request_content, response_content,
                    model_call_count, prompt_tokens, completion_tokens, total_tokens,
                    usage_complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture.request_id,
                    capture.device_key,
                    capture.route,
                    capture.pipeline,
                    _utc_iso(capture.started_at),
                    _utc_iso(capture.completed_at),
                    capture.duration_ms,
                    capture.status_code,
                    _json_dump(capture.request_content),
                    None if capture.response_content is None else _json_dump(capture.response_content),
                    len(capture.model_calls),
                    None if usage is None else usage.prompt_tokens,
                    None if usage is None else usage.completion_tokens,
                    None if usage is None else usage.total_tokens,
                    int(capture.usage_complete),
                ),
            )
            inference_id = int(cursor.lastrowid)
            self._connection.executemany(
                """
                INSERT INTO model_calls (
                    inference_request_id, call_index, pipeline, started_at, completed_at,
                    duration_ms, input_content, output_content, provider_model,
                    prompt_tokens, completion_tokens, total_tokens, usage_complete,
                    error_type, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_call_values(inference_id, call) for call in capture.model_calls],
            )
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._content_retention_days)
            self._connection.execute(
                """UPDATE inference_requests SET request_content = NULL, response_content = NULL
                WHERE completed_at < ? AND (request_content IS NOT NULL OR response_content IS NOT NULL)""",
                (_utc_iso(cutoff),),
            )
            self._connection.execute(
                """UPDATE model_calls SET input_content = NULL, output_content = NULL
                WHERE completed_at < ? AND (input_content IS NOT NULL OR output_content IS NOT NULL)""",
                (_utc_iso(cutoff),),
            )

    def list_requests(
        self,
        *,
        device_key: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        where, parameters = _filters(device_key, start_time, end_time)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM inference_requests{where}", parameters
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"""SELECT * FROM inference_requests{where}
                ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
            records = [self._record_from_row(row) for row in rows]
        return records, total

    def summarize(
        self,
        *,
        device_key: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        where, parameters = _filters(device_key, start_time, end_time)
        columns = """
            COUNT(*) AS request_count,
            SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS successful_requests,
            SUM(CASE WHEN status_code < 200 OR status_code > 299 THEN 1 ELSE 0 END) AS failed_requests,
            COALESCE(SUM(model_call_count), 0) AS model_call_count,
            SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS token_reported_requests,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(ROUND(AVG(duration_ms), 2), 0) AS average_duration_ms,
            MIN(started_at) AS first_request_at,
            MAX(started_at) AS last_request_at
        """
        with self._lock:
            total_row = self._connection.execute(
                f"SELECT {columns} FROM inference_requests{where}", parameters
            ).fetchone()
            device_rows = self._connection.execute(
                f"""SELECT device_key, {columns} FROM inference_requests{where}
                GROUP BY device_key ORDER BY total_tokens DESC, request_count DESC, device_key""",
                parameters,
            ).fetchall()
        return _summary_from_row(total_row), [
            {"device_key": row["device_key"], **_summary_from_row(row)} for row in device_rows
        ]

    def _record_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        call_rows = self._connection.execute(
            "SELECT * FROM model_calls WHERE inference_request_id = ? ORDER BY call_index",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "device_key": row["device_key"],
            "route": row["route"],
            "pipeline": row["pipeline"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "duration_ms": row["duration_ms"],
            "status_code": row["status_code"],
            "request_content": _json_load(row["request_content"]),
            "response_content": _json_load(row["response_content"]),
            "model_call_count": row["model_call_count"],
            "usage": _usage_from_row(row),
            "usage_complete": bool(row["usage_complete"]),
            "model_calls": [_model_call_from_row(call) for call in call_rows],
        }


def _call_values(inference_id: int, call: ModelCallCapture) -> tuple[Any, ...]:
    usage = call.usage
    return (
        inference_id,
        call.call_index,
        call.pipeline,
        _utc_iso(call.started_at),
        _utc_iso(call.completed_at),
        call.duration_ms,
        call.input_content,
        call.output_content,
        call.provider_model,
        None if usage is None else usage.prompt_tokens,
        None if usage is None else usage.completion_tokens,
        None if usage is None else usage.total_tokens,
        int(call.usage_complete),
        call.error_type,
        call.error_message,
    )


def _filters(
    device_key: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[str, tuple[Any, ...]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    for column, operator, value in (
        ("device_key", "=", device_key),
        ("started_at", ">=", None if start_time is None else _utc_iso(start_time)),
        ("started_at", "<", None if end_time is None else _utc_iso(end_time)),
    ):
        if value is not None:
            conditions.append(f"{column} {operator} ?")
            parameters.append(value)
    return (" WHERE " + " AND ".join(conditions) if conditions else "", tuple(parameters))


def _model_call_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "call_index": row["call_index"],
        "pipeline": row["pipeline"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "duration_ms": row["duration_ms"],
        "input_content": row["input_content"],
        "output_content": row["output_content"],
        "provider_model": row["provider_model"],
        "usage": _usage_from_row(row),
        "usage_complete": bool(row["usage_complete"]),
        "error_type": row["error_type"],
        "error_message": row["error_message"],
    }


def _usage_from_row(row: sqlite3.Row) -> dict[str, int] | None:
    if row["total_tokens"] is None:
        return None
    return {
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
    }


def _summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "request_count": int(row["request_count"] or 0),
        "successful_requests": int(row["successful_requests"] or 0),
        "failed_requests": int(row["failed_requests"] or 0),
        "model_call_count": int(row["model_call_count"] or 0),
        "token_reported_requests": int(row["token_reported_requests"] or 0),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "average_duration_ms": float(row["average_duration_ms"] or 0),
        "first_request_at": row["first_request_at"],
        "last_request_at": row["last_request_at"],
    }


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
