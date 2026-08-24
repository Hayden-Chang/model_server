from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)

    @field_validator("input")
    @classmethod
    def input_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("input must not be blank")
        return value


class ModelMetadata(BaseModel):
    alias: str
    provider_model: str | None = None
    usage: dict[str, Any] | None = None


class RunResponse(BaseModel):
    pipeline: str
    request_id: str
    result: str | dict[str, Any]
    model: ModelMetadata


class TimeFragmentGuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=16, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


class TimeFragmentGuestResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class TimeFragmentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    start: str | None
    end: str | None
    status: Literal["scheduled", "active", "done", "skipped"]

    @field_validator("id", "title")
    @classmethod
    def identity_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task id and title must not be blank")
        return value

    @model_validator(mode="after")
    def times_must_both_be_present_or_absent(self) -> "TimeFragmentTask":
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must both be present or absent")
        return self


class TimeFragmentCurrentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    date: str
    state: Literal["PLANNING", "RUNNING", "SLOWED", "DONE"]
    task_ids: list[str] = Field(alias="taskIds")
    tasks: list[TimeFragmentTask]
    checkins: list[dict[str, Any]]


class TimeFragmentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str = Field(min_length=1, max_length=4_000)
    current_plan: TimeFragmentCurrentPlan | None = Field(alias="currentPlan")
    now: str

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value

    @field_validator("now")
    @classmethod
    def now_must_include_timezone(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("now must be an ISO 8601 datetime") from error
        if parsed.tzinfo is None:
            raise ValueError("now must include a timezone offset")
        return value


class TimeFragmentPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TimeFragmentTask] = Field(min_length=1, max_length=96)

    @model_validator(mode="after")
    def task_ids_must_be_unique(self) -> "TimeFragmentPlanResponse":
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        return self
