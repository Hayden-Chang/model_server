from datetime import date as Date
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

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
    task_ids: list[str] = Field(alias="taskIds")
    tasks: list[TimeFragmentTask]
    checkins: list[dict[str, Any]]


class TimeFragmentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str = Field(min_length=1, max_length=4_000)
    current_plan: TimeFragmentCurrentPlan = Field(alias="currentPlan")
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


# The v2 types intentionally live alongside the route-facing v1 types above.
# WP3 can switch the route atomically without changing the legacy contract first.


class _TimeFragmentV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TimeFragmentSegmentV2(_TimeFragmentV2Model):
    start_slot: int = Field(alias="startSlot", ge=0, le=95, strict=True)
    end_slot: int = Field(alias="endSlot", ge=1, le=96, strict=True)

    @model_validator(mode="after")
    def start_must_precede_end(self) -> "TimeFragmentSegmentV2":
        if self.start_slot >= self.end_slot:
            raise ValueError("segment startSlot must be before endSlot")
        return self


class TimeFragmentInternalDomainRef(_TimeFragmentV2Model):
    task_id: str = Field(alias="taskId", min_length=1, max_length=200)
    occurrence_id: str = Field(alias="occurrenceId", min_length=1, max_length=200)
    scheduled_task_id: str | None = Field(
        default=None,
        alias="scheduledTaskId",
        min_length=1,
        max_length=200,
    )


class TimeFragmentExternalDomainRef(_TimeFragmentV2Model):
    external_event_id: str = Field(alias="externalEventId", min_length=1, max_length=200)


class TimeFragmentInternalTaskItem(_TimeFragmentV2Model):
    item_id: str = Field(alias="itemId", min_length=1, max_length=200)
    object_type: Literal["internalTask"] = Field(alias="objectType")
    domain_ref: TimeFragmentInternalDomainRef | None = Field(alias="domainRef")
    title: str = Field(min_length=1, max_length=500)
    duration_slots: int = Field(alias="durationSlots", ge=1, le=96, strict=True)
    segments: list[TimeFragmentSegmentV2] = Field(max_length=96)
    is_pinned: bool = Field(alias="isPinned", strict=True)
    is_completed: bool = Field(alias="isCompleted", strict=True)

    @field_validator("item_id", "title")
    @classmethod
    def identity_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("itemId and title must not be blank")
        return value

    @model_validator(mode="after")
    def occurrence_reference_must_match_item(self) -> "TimeFragmentInternalTaskItem":
        if self.domain_ref is not None and self.domain_ref.occurrence_id != self.item_id:
            raise ValueError("domainRef.occurrenceId must match itemId")
        return self


class TimeFragmentExternalEventItem(_TimeFragmentV2Model):
    item_id: str = Field(alias="itemId", min_length=1, max_length=200)
    object_type: Literal["externalEvent"] = Field(alias="objectType")
    domain_ref: TimeFragmentExternalDomainRef = Field(alias="domainRef")
    title: str = Field(min_length=1, max_length=500)
    duration_slots: int = Field(alias="durationSlots", ge=1, le=96, strict=True)
    segments: list[TimeFragmentSegmentV2] = Field(max_length=96)
    is_all_day: bool = Field(alias="isAllDay", strict=True)
    is_fixed: bool = Field(alias="isFixed", strict=True)

    @field_validator("item_id", "title")
    @classmethod
    def identity_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("itemId and title must not be blank")
        return value

    @model_validator(mode="after")
    def external_reference_must_match_item(self) -> "TimeFragmentExternalEventItem":
        if self.domain_ref.external_event_id != self.item_id:
            raise ValueError("domainRef.externalEventId must match itemId")
        return self


TimeFragmentPlanItem = Annotated[
    TimeFragmentInternalTaskItem | TimeFragmentExternalEventItem,
    Field(discriminator="object_type"),
]


class TimeFragmentPlanV2(_TimeFragmentV2Model):
    date: str
    items: list[TimeFragmentPlanItem]

    @field_validator("date")
    @classmethod
    def date_must_be_iso_local_date(cls, value: str) -> str:
        try:
            parsed = Date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("date must use YYYY-MM-DD format") from error
        if parsed.isoformat() != value:
            raise ValueError("date must use YYYY-MM-DD format")
        return value


class TimeFragmentPlanRequestV2(_TimeFragmentV2Model):
    text: str = Field(min_length=1, max_length=4_000)
    request_id: str = Field(alias="requestID", min_length=1, max_length=200)
    base_fingerprint: str = Field(alias="baseFingerprint", min_length=1, max_length=500)
    current_plan: TimeFragmentPlanV2 = Field(alias="currentPlan")
    now: str

    @field_validator("text", "request_id", "base_fingerprint")
    @classmethod
    def request_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request fields must not be blank")
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


class TimeFragmentModelVisibleInternalTask(_TimeFragmentV2Model):
    item_id: str = Field(alias="itemId")
    object_type: Literal["internalTask"] = Field(alias="objectType")
    title: str
    duration_slots: int = Field(alias="durationSlots")
    segments: list[TimeFragmentSegmentV2]
    is_pinned: bool = Field(alias="isPinned")
    is_completed: bool = Field(alias="isCompleted")


class TimeFragmentModelVisibleExternalEvent(_TimeFragmentV2Model):
    item_id: str = Field(alias="itemId")
    object_type: Literal["externalEvent"] = Field(alias="objectType")
    title: str
    duration_slots: int = Field(alias="durationSlots")
    segments: list[TimeFragmentSegmentV2]
    is_all_day: bool = Field(alias="isAllDay")
    is_fixed: bool = Field(alias="isFixed")


TimeFragmentModelVisibleItem = Annotated[
    TimeFragmentModelVisibleInternalTask | TimeFragmentModelVisibleExternalEvent,
    Field(discriminator="object_type"),
]


class TimeFragmentModelVisiblePlan(_TimeFragmentV2Model):
    date: str
    items: list[TimeFragmentModelVisibleItem]


class TimeFragmentModelPlanRequest(_TimeFragmentV2Model):
    text: str
    current_plan: TimeFragmentModelVisiblePlan = Field(alias="currentPlan")
    now: str


class TimeFragmentPlacement(_TimeFragmentV2Model):
    anchor: Literal["start", "end"]
    slot: int = Field(ge=0, le=96, strict=True)


TimeFragmentObjectType = Literal["internalTask", "externalEvent"]


class _TimeFragmentExistingModelOperation(_TimeFragmentV2Model):
    target_item_id: str = Field(alias="targetItemId", min_length=1, max_length=200)
    object_type: TimeFragmentObjectType | None = Field(default=None, alias="objectType")
    priority: int | None = Field(default=None, strict=True)
    input_order: int = Field(alias="inputOrder", ge=0, strict=True)
    authorization_text: str | None = Field(
        default=None,
        alias="authorizationText",
        min_length=1,
        max_length=1_000,
    )


class TimeFragmentModelAddOperation(_TimeFragmentV2Model):
    type: Literal["add"]
    title: str = Field(min_length=1, max_length=500)
    duration_slots: int = Field(default=2, alias="durationSlots", ge=1, le=96, strict=True)
    placement: TimeFragmentPlacement | None = None
    priority: int | None = Field(default=None, strict=True)
    input_order: int = Field(alias="inputOrder", ge=0, strict=True)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value


class TimeFragmentModelMoveOperation(_TimeFragmentExistingModelOperation):
    type: Literal["move"]
    allowed_changes: list[Literal["segments"]] = Field(
        alias="allowedChanges",
        min_length=1,
        max_length=1,
    )
    placement: TimeFragmentPlacement | None = None

    @field_validator("allowed_changes")
    @classmethod
    def only_segments_may_change(cls, value: list[str]) -> list[str]:
        if value != ["segments"]:
            raise ValueError("move allowedChanges must be exactly ['segments']")
        return value


class TimeFragmentModelChangeDurationOperation(_TimeFragmentExistingModelOperation):
    type: Literal["changeDuration"]
    allowed_changes: list[Literal["durationSlots", "segments"]] = Field(
        alias="allowedChanges",
        min_length=2,
        max_length=2,
    )
    duration_slots: int = Field(alias="durationSlots", ge=1, le=96, strict=True)

    @field_validator("allowed_changes")
    @classmethod
    def duration_and_segments_may_change(cls, value: list[str]) -> list[str]:
        if len(value) != 2 or set(value) != {"durationSlots", "segments"}:
            raise ValueError(
                "changeDuration allowedChanges must contain durationSlots and segments exactly once"
            )
        return value


class TimeFragmentModelChangeTitleOperation(_TimeFragmentV2Model):
    type: Literal["changeTitle"]
    target_item_id: str = Field(alias="targetItemId", min_length=1, max_length=200)
    object_type: Literal["internalTask"] = Field(alias="objectType")
    title: str = Field(min_length=1, max_length=500)
    allowed_changes: list[Literal["title"]] = Field(
        alias="allowedChanges",
        min_length=1,
        max_length=1,
    )
    input_order: int = Field(alias="inputOrder", ge=0, strict=True)
    authorization_text: str | None = Field(
        default=None,
        alias="authorizationText",
        min_length=1,
        max_length=1_000,
    )

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("allowed_changes")
    @classmethod
    def only_title_may_change(cls, value: list[str]) -> list[str]:
        if value != ["title"]:
            raise ValueError("changeTitle allowedChanges must be exactly ['title']")
        return value


class TimeFragmentModelDeleteOperation(_TimeFragmentExistingModelOperation):
    type: Literal["delete"]
    allowed_changes: list[Literal["item"]] = Field(
        default_factory=list,
        alias="allowedChanges",
        max_length=0,
    )

    @field_validator("allowed_changes")
    @classmethod
    def delete_must_not_authorize_field_changes(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError("delete allowedChanges must be empty")
        return value


TimeFragmentModelOperation = Annotated[
    TimeFragmentModelAddOperation
    | TimeFragmentModelMoveOperation
    | TimeFragmentModelChangeDurationOperation
    | TimeFragmentModelChangeTitleOperation
    | TimeFragmentModelDeleteOperation,
    Field(discriminator="type"),
]


class TimeFragmentModelOperations(_TimeFragmentV2Model):
    operations: list[TimeFragmentModelOperation]


class TimeFragmentAddOperation(TimeFragmentModelAddOperation):
    temporary_id: UUID = Field(alias="temporaryId")


class _TimeFragmentExistingOperation(_TimeFragmentV2Model):
    target_item_id: str = Field(alias="targetItemId", min_length=1, max_length=200)
    object_type: TimeFragmentObjectType | None = Field(default=None, alias="objectType")
    priority: int | None = Field(default=None, strict=True)
    input_order: int = Field(alias="inputOrder", ge=0, strict=True)


class TimeFragmentMoveOperation(_TimeFragmentExistingOperation):
    type: Literal["move"]
    allowed_changes: list[Literal["segments"]] = Field(
        alias="allowedChanges",
        min_length=1,
        max_length=1,
    )
    placement: TimeFragmentPlacement | None = None

    @field_validator("allowed_changes")
    @classmethod
    def only_segments_may_change(cls, value: list[str]) -> list[str]:
        if value != ["segments"]:
            raise ValueError("move allowedChanges must be exactly ['segments']")
        return value


class TimeFragmentChangeDurationOperation(_TimeFragmentExistingOperation):
    type: Literal["changeDuration"]
    allowed_changes: list[Literal["durationSlots", "segments"]] = Field(
        alias="allowedChanges",
        min_length=2,
        max_length=2,
    )
    duration_slots: int = Field(alias="durationSlots", ge=1, le=96, strict=True)

    @field_validator("allowed_changes")
    @classmethod
    def duration_and_segments_may_change(cls, value: list[str]) -> list[str]:
        if len(value) != 2 or set(value) != {"durationSlots", "segments"}:
            raise ValueError(
                "changeDuration allowedChanges must contain durationSlots and segments exactly once"
            )
        return value


class TimeFragmentChangeTitleOperation(_TimeFragmentV2Model):
    type: Literal["changeTitle"]
    target_item_id: str = Field(alias="targetItemId", min_length=1, max_length=200)
    object_type: Literal["internalTask"] = Field(alias="objectType")
    title: str = Field(min_length=1, max_length=500)
    allowed_changes: list[Literal["title"]] = Field(
        alias="allowedChanges",
        min_length=1,
        max_length=1,
    )
    input_order: int = Field(alias="inputOrder", ge=0, strict=True)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("allowed_changes")
    @classmethod
    def only_title_may_change(cls, value: list[str]) -> list[str]:
        if value != ["title"]:
            raise ValueError("changeTitle allowedChanges must be exactly ['title']")
        return value


class TimeFragmentDeleteOperation(_TimeFragmentExistingOperation):
    type: Literal["delete"]
    allowed_changes: list[Literal["item"]] = Field(
        default_factory=list,
        alias="allowedChanges",
        max_length=0,
    )


TimeFragmentOperation = Annotated[
    TimeFragmentAddOperation
    | TimeFragmentMoveOperation
    | TimeFragmentChangeDurationOperation
    | TimeFragmentChangeTitleOperation
    | TimeFragmentDeleteOperation,
    Field(discriminator="type"),
]


class TimeFragmentPlanProposal(_TimeFragmentV2Model):
    base_fingerprint: str = Field(alias="baseFingerprint")
    algorithm_version: Literal["time-fragment-planner-v1"] = Field(alias="algorithmVersion")
    deleted_occurrence_ids: list[str] = Field(alias="deletedOccurrenceIDs")
    deleted_external_event_ids: list[str] = Field(alias="deletedExternalEventIDs")
    operations: list[TimeFragmentOperation]
    candidate_plan: TimeFragmentPlanV2 = Field(alias="candidatePlan")


class TimeFragmentValidationIssue(_TimeFragmentV2Model):
    source: Literal["model_server", "time_fragment"]
    severity: Literal["error", "warning"]
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    item_id: str | None = Field(default=None, alias="itemId")
    field: str | None = None


class TimeFragmentValidation(_TimeFragmentV2Model):
    valid: bool
    attempts: Literal[1, 2]
    issues: list[TimeFragmentValidationIssue]

    @model_validator(mode="after")
    def valid_must_match_error_issues(self) -> "TimeFragmentValidation":
        if self.valid == any(issue.severity == "error" for issue in self.issues):
            raise ValueError("valid must be false exactly when an error issue exists")
        return self


class TimeFragmentPlanResponseV2(_TimeFragmentV2Model):
    request_id: str = Field(alias="requestID")
    proposal: TimeFragmentPlanProposal | None
    validation: TimeFragmentValidation
