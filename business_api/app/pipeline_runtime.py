import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .pipelines import Pipeline, get_pipeline


CONFIGURABLE_PIPELINES = frozenset({"time-fragment-plan-v2"})
MODEL_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
ThinkingMode = Literal["enabled", "disabled"]
ReasoningEffort = Literal["low", "high", "max"]


class PipelineRuntimeNotConfigurable(Exception):
    pass


class PipelineRuntimeInvalidConfig(Exception):
    pass


class PipelineRuntimeVersionConflict(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected version {expected}, current version is {actual}")
        self.expected = expected
        self.actual = actual


class PipelineRuntimeNoPreviousVersion(Exception):
    pass


@dataclass(frozen=True)
class PipelineRuntimeConfig:
    pipeline_id: str
    model_alias: str
    thinking_mode: ThinkingMode
    reasoning_effort: ReasoningEffort | None
    version: int
    source: Literal["default", "override"]
    updated_at: datetime | None


class PipelineRuntimeStore:
    def __init__(self, database_path: str, default_model_alias: str) -> None:
        if database_path != ":memory:":
            database_file = Path(database_path).expanduser()
            database_file.parent.mkdir(parents=True, exist_ok=True)
            database_file.touch(mode=0o600, exist_ok=True)
            database_file.chmod(0o600)
        self._default_model_alias = default_model_alias
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if database_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runtime_configs (
                    pipeline_id TEXT PRIMARY KEY,
                    model_alias TEXT NOT NULL,
                    thinking_mode TEXT NOT NULL CHECK (thinking_mode IN ('enabled', 'disabled')),
                    reasoning_effort TEXT CHECK (reasoning_effort IN ('low', 'high', 'max')),
                    version INTEGER NOT NULL CHECK (version >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pipeline_runtime_config_history (
                    pipeline_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    model_alias TEXT NOT NULL,
                    thinking_mode TEXT NOT NULL CHECK (thinking_mode IN ('enabled', 'disabled')),
                    reasoning_effort TEXT CHECK (reasoning_effort IN ('low', 'high', 'max')),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (pipeline_id, version)
                );
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get(self, pipeline_id: str) -> PipelineRuntimeConfig:
        self._require_configurable(pipeline_id)
        with self._lock:
            return self._current_unlocked(pipeline_id)

    def resolve(self, pipeline_id: str) -> Pipeline | None:
        pipeline = get_pipeline(pipeline_id)
        if pipeline is None or pipeline_id not in CONFIGURABLE_PIPELINES:
            return pipeline
        config = self.get(pipeline_id)
        return replace(
            pipeline,
            model_alias=config.model_alias,
            thinking_mode=config.thinking_mode,
            reasoning_effort=config.reasoning_effort,
        )

    def update(
        self,
        pipeline_id: str,
        *,
        model_alias: str,
        thinking_mode: ThinkingMode,
        reasoning_effort: ReasoningEffort | None,
        expected_version: int,
    ) -> PipelineRuntimeConfig:
        self._require_configurable(pipeline_id)
        _validate_config(model_alias, thinking_mode, reasoning_effort)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._current_unlocked(pipeline_id)
                if current.version != expected_version:
                    raise PipelineRuntimeVersionConflict(expected_version, current.version)
                if (
                    current.model_alias == model_alias
                    and current.thinking_mode == thinking_mode
                    and current.reasoning_effort == reasoning_effort
                ):
                    self._connection.rollback()
                    return current
                config = self._write_unlocked(
                    pipeline_id,
                    model_alias=model_alias,
                    thinking_mode=thinking_mode,
                    reasoning_effort=reasoning_effort,
                    version=current.version + 1,
                )
                self._connection.commit()
                return config
            except Exception:
                self._connection.rollback()
                raise

    def rollback(self, pipeline_id: str, *, expected_version: int) -> PipelineRuntimeConfig:
        self._require_configurable(pipeline_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._current_unlocked(pipeline_id)
                if current.version != expected_version:
                    raise PipelineRuntimeVersionConflict(expected_version, current.version)
                if current.version == 0:
                    raise PipelineRuntimeNoPreviousVersion(
                        "pipeline is already using its default configuration"
                    )
                target_version = current.version - 1
                if target_version == 0:
                    target = self._default(pipeline_id)
                else:
                    row = self._connection.execute(
                        """SELECT * FROM pipeline_runtime_config_history
                        WHERE pipeline_id = ? AND version = ?""",
                        (pipeline_id, target_version),
                    ).fetchone()
                    if row is None:
                        raise PipelineRuntimeNoPreviousVersion(
                            "previous pipeline configuration is unavailable"
                        )
                    target = _config_from_row(row)
                config = self._write_unlocked(
                    pipeline_id,
                    model_alias=target.model_alias,
                    thinking_mode=target.thinking_mode,
                    reasoning_effort=target.reasoning_effort,
                    version=current.version + 1,
                )
                self._connection.commit()
                return config
            except Exception:
                self._connection.rollback()
                raise

    def history(self, pipeline_id: str, *, limit: int = 50) -> list[PipelineRuntimeConfig]:
        self._require_configurable(pipeline_id)
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM pipeline_runtime_config_history
                WHERE pipeline_id = ? ORDER BY version DESC LIMIT ?""",
                (pipeline_id, limit),
            ).fetchall()
        records = [_config_from_row(row) for row in rows]
        if len(records) < limit:
            records.append(self._default(pipeline_id))
        return records

    def _default(self, pipeline_id: str) -> PipelineRuntimeConfig:
        pipeline = get_pipeline(pipeline_id)
        assert pipeline is not None
        assert pipeline.thinking_mode is not None
        return PipelineRuntimeConfig(
            pipeline_id=pipeline_id,
            model_alias=pipeline.model_alias or self._default_model_alias,
            thinking_mode=pipeline.thinking_mode,
            reasoning_effort=pipeline.reasoning_effort,
            version=0,
            source="default",
            updated_at=None,
        )

    def _current_unlocked(self, pipeline_id: str) -> PipelineRuntimeConfig:
        row = self._connection.execute(
            "SELECT * FROM pipeline_runtime_configs WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        return self._default(pipeline_id) if row is None else _config_from_row(row)

    def _write_unlocked(
        self,
        pipeline_id: str,
        *,
        model_alias: str,
        thinking_mode: ThinkingMode,
        reasoning_effort: ReasoningEffort | None,
        version: int,
    ) -> PipelineRuntimeConfig:
        updated_at = datetime.now(timezone.utc)
        updated_at_text = _utc_iso(updated_at)
        self._connection.execute(
            """INSERT INTO pipeline_runtime_configs (
                pipeline_id, model_alias, thinking_mode, reasoning_effort, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                model_alias = excluded.model_alias,
                thinking_mode = excluded.thinking_mode,
                reasoning_effort = excluded.reasoning_effort,
                version = excluded.version,
                updated_at = excluded.updated_at""",
            (pipeline_id, model_alias, thinking_mode, reasoning_effort, version, updated_at_text),
        )
        self._connection.execute(
            """INSERT INTO pipeline_runtime_config_history (
                pipeline_id, version, model_alias, thinking_mode, reasoning_effort, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (pipeline_id, version, model_alias, thinking_mode, reasoning_effort, updated_at_text),
        )
        return PipelineRuntimeConfig(
            pipeline_id=pipeline_id,
            model_alias=model_alias,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            version=version,
            source="override",
            updated_at=updated_at,
        )

    @staticmethod
    def _require_configurable(pipeline_id: str) -> None:
        if pipeline_id not in CONFIGURABLE_PIPELINES:
            raise PipelineRuntimeNotConfigurable(pipeline_id)


def _validate_config(
    model_alias: str,
    thinking_mode: ThinkingMode,
    reasoning_effort: ReasoningEffort | None,
) -> None:
    if MODEL_ALIAS_PATTERN.fullmatch(model_alias) is None:
        raise PipelineRuntimeInvalidConfig("invalid model alias")
    if thinking_mode == "enabled" and reasoning_effort is None:
        raise PipelineRuntimeInvalidConfig("enabled thinking requires a reasoning effort")
    if thinking_mode == "disabled" and reasoning_effort is not None:
        raise PipelineRuntimeInvalidConfig("disabled thinking cannot use a reasoning effort")


def _config_from_row(row: sqlite3.Row) -> PipelineRuntimeConfig:
    return PipelineRuntimeConfig(
        pipeline_id=row["pipeline_id"],
        model_alias=row["model_alias"],
        thinking_mode=row["thinking_mode"],
        reasoning_effort=row["reasoning_effort"],
        version=row["version"],
        source="override",
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
