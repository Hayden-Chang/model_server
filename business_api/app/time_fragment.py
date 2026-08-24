from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal
from uuid import UUID, uuid4

from .contracts import (
    TimeFragmentAddOperation,
    TimeFragmentChangeDurationOperation,
    TimeFragmentDeleteOperation,
    TimeFragmentExternalEventItem,
    TimeFragmentInternalTaskItem,
    TimeFragmentModelAddOperation,
    TimeFragmentModelOperation,
    TimeFragmentModelOperations,
    TimeFragmentModelPlanRequest,
    TimeFragmentModelVisibleExternalEvent,
    TimeFragmentModelVisibleInternalTask,
    TimeFragmentModelVisiblePlan,
    TimeFragmentMoveOperation,
    TimeFragmentOperation,
    TimeFragmentPlacement,
    TimeFragmentPlanItem,
    TimeFragmentPlanProposal,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanResponse,
    TimeFragmentPlanResponseV2,
    TimeFragmentPlanV2,
    TimeFragmentSegmentV2,
    TimeFragmentValidation,
    TimeFragmentValidationIssue,
)
from .postprocessors import ModelOutputInvalid


TIME_FRAGMENT_PLANNER_VERSION = "time-fragment-planner-v1"


def validate_plan_for_request(plan: TimeFragmentPlanResponse, now: str) -> None:
    local_day = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    scheduled: list[tuple[datetime, datetime]] = []

    for task in plan.tasks:
        if task.start is None or task.end is None:
            continue
        try:
            start = datetime.fromisoformat(task.start)
            end = datetime.fromisoformat(task.end)
        except ValueError as error:
            raise ModelOutputInvalid("task time is not a valid ISO 8601 datetime") from error
        if (
            start.strftime("%Y-%m-%dT%H:%M:%S") != task.start
            or end.strftime("%Y-%m-%dT%H:%M:%S") != task.end
        ):
            raise ModelOutputInvalid("task times must use local YYYY-MM-DDTHH:MM:SS format")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ModelOutputInvalid("task times must be local datetimes without timezone suffixes")
        if start.date() != local_day or end.date() != local_day:
            raise ModelOutputInvalid("task falls outside the request's local day")
        if start >= end:
            raise ModelOutputInvalid("task start must be before end")
        alignment_parts = (
            start.minute % 15,
            end.minute % 15,
            start.second,
            end.second,
            start.microsecond,
            end.microsecond,
        )
        if any(alignment_parts):
            raise ModelOutputInvalid("task times must align to 15-minute boundaries")
        scheduled.append((start, end))

    if scheduled != sorted(scheduled):
        raise ModelOutputInvalid("scheduled tasks must be sorted by start time")
    for (_, previous_end), (next_start, _) in zip(scheduled, scheduled[1:]):
        if previous_end > next_start:
            raise ModelOutputInvalid("scheduled tasks must not overlap")


def project_time_fragment_request_for_model(
    request: TimeFragmentPlanRequestV2,
) -> TimeFragmentModelPlanRequest:
    """Return the only request projection that may be serialized into a model prompt."""

    visible_items = []
    for item in request.current_plan.items:
        item_data = item.model_dump(mode="json", by_alias=True)
        item_data.pop("domainRef")
        if isinstance(item, TimeFragmentInternalTaskItem):
            visible_items.append(TimeFragmentModelVisibleInternalTask.model_validate(item_data))
        else:
            visible_items.append(TimeFragmentModelVisibleExternalEvent.model_validate(item_data))
    return TimeFragmentModelPlanRequest(
        text=request.text,
        currentPlan=TimeFragmentModelVisiblePlan(
            date=request.current_plan.date,
            items=visible_items,
        ),
        now=request.now,
    )


def build_time_fragment_parse_failed_response(
    request_id: str,
    *,
    attempts: Literal[1, 2],
    message: str = "大模型返回的调整内容无法解析",
) -> TimeFragmentPlanResponseV2:
    return TimeFragmentPlanResponseV2(
        requestID=request_id,
        proposal=None,
        validation=TimeFragmentValidation(
            valid=False,
            attempts=attempts,
            issues=[
                TimeFragmentValidationIssue(
                    source="model_server",
                    severity="error",
                    code="PARSE_FAILED",
                    message=message,
                )
            ],
        ),
    )


@dataclass
class _ScheduleTarget:
    item_id: str
    placement: TimeFragmentPlacement | None
    placement_is_explicit: bool
    priority: int | None
    input_order: int
    operation_index: int


def plan_time_fragment(
    request: TimeFragmentPlanRequestV2,
    model_output: TimeFragmentModelOperations,
    *,
    attempts: Literal[1, 2] = 1,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> TimeFragmentPlanResponseV2:
    """Expand parsed model operations into a complete, deterministic candidate plan.

    Semantic failures are represented as validation issues while retaining the
    candidate. Structural model-output failures are handled by WP3 with
    ``build_time_fragment_parse_failed_response``.
    """

    base_items = [item.model_copy(deep=True) for item in request.current_plan.items]
    base_counts = Counter(item.item_id for item in base_items)
    base_index = {item.item_id: item for item in base_items if base_counts[item.item_id] == 1}
    normalized_operations = _normalize_operations(
        model_output,
        base_index,
        existing_item_ids=set(base_counts),
        uuid_factory=uuid_factory,
    )
    issues: list[TimeFragmentValidationIssue] = []
    if any(count > 1 for count in base_counts.values()):
        _add_issue(
            issues,
            code="ID_SET_MISMATCH",
            message="currentPlan 包含重复 itemId",
            field="currentPlan.items",
        )

    operations_by_target: dict[
        str,
        list[tuple[int, TimeFragmentOperation, TimeFragmentModelOperation]],
    ] = {}
    for index, (operation, model_operation) in enumerate(
        zip(normalized_operations, model_output.operations, strict=True)
    ):
        if not isinstance(operation, TimeFragmentAddOperation):
            operations_by_target.setdefault(operation.target_item_id, []).append(
                (index, operation, model_operation)
            )

    invalid_targets: set[str] = set()
    for item_id, indexed_operations in operations_by_target.items():
        operation_types = [operation.type for _, operation, _ in indexed_operations]
        if len(operation_types) != len(set(operation_types)) or (
            "delete" in operation_types and len(operation_types) > 1
        ):
            invalid_targets.add(item_id)
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标存在重复或互相矛盾的操作",
                item_id=item_id,
                field="operations",
            )

    candidate_items = [item.model_copy(deep=True) for item in base_items]
    candidate_by_id = {item.item_id: item for item in candidate_items if base_counts[item.item_id] == 1}
    candidate_deleted_ids: set[str] = set()
    deleted_occurrence_ids: list[str] = []
    deleted_external_event_ids: list[str] = []
    schedule_targets: list[_ScheduleTarget] = []

    for item_id, indexed_operations in operations_by_target.items():
        target = base_index.get(item_id)
        if target is None:
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作引用的 itemId 不存在于 currentPlan",
                item_id=item_id,
                field="targetItemId",
            )
            continue
        if item_id in invalid_targets:
            continue
        if any(
            operation.object_type is not None and operation.object_type != target.object_type
            for _, operation, _ in indexed_operations
        ):
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作 objectType 与 currentPlan 目标不匹配",
                item_id=item_id,
                field="objectType",
            )
            continue

        operations = [operation for _, operation, _ in indexed_operations]
        if _is_protected(target) and any(
            not _protected_operation_is_authorized(
                request.text,
                target,
                operation,
                getattr(model_operation, "authorization_text", None),
            )
            for _, operation, model_operation in indexed_operations
        ):
            _add_issue(
                issues,
                code="PROTECTED_OBJECT",
                message=f"用户没有明确授权修改受保护对象「{target.title}」",
                item_id=item_id,
                field="operations",
            )

        if isinstance(operations[0], TimeFragmentDeleteOperation):
            candidate_deleted_ids.add(item_id)
            if isinstance(target, TimeFragmentInternalTaskItem) and target.domain_ref is not None:
                deleted_occurrence_ids.append(item_id)
            elif isinstance(target, TimeFragmentExternalEventItem):
                deleted_external_event_ids.append(item_id)
            continue

        candidate = candidate_by_id[item_id]
        original_segments = list(candidate.segments)
        placement: TimeFragmentPlacement | None = None
        priorities = {operation.priority for operation in operations if operation.priority is not None}
        if len(priorities) > 1:
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标的操作包含冲突的 priority",
                item_id=item_id,
                field="priority",
            )
            continue
        for operation in operations:
            if isinstance(operation, TimeFragmentChangeDurationOperation):
                candidate = candidate.model_copy(
                    update={"duration_slots": operation.duration_slots},
                    deep=True,
                )
            elif isinstance(operation, TimeFragmentMoveOperation):
                placement = operation.placement
        if placement is None and not any(
            isinstance(operation, TimeFragmentMoveOperation) for operation in operations
        ):
            if original_segments:
                placement = TimeFragmentPlacement(
                    anchor="start",
                    slot=min(segment.start_slot for segment in original_segments),
                )
        candidate = candidate.model_copy(update={"segments": []}, deep=True)
        _replace_item(candidate_items, candidate)
        candidate_by_id[item_id] = candidate
        schedule_targets.append(
            _ScheduleTarget(
                item_id=item_id,
                placement=placement,
                placement_is_explicit=any(
                    isinstance(operation, TimeFragmentMoveOperation)
                    and operation.placement is not None
                    for operation in operations
                ),
                priority=next(iter(priorities), None),
                input_order=min(operation.input_order for operation in operations),
                operation_index=min(index for index, _, _ in indexed_operations),
            )
        )

    candidate_items = [item for item in candidate_items if item.item_id not in candidate_deleted_ids]
    candidate_by_id = {item.item_id: item for item in candidate_items}

    for operation_index, operation in enumerate(normalized_operations):
        if not isinstance(operation, TimeFragmentAddOperation):
            continue
        item_id = str(operation.temporary_id)
        added_item = TimeFragmentInternalTaskItem(
            itemId=item_id,
            objectType="internalTask",
            domainRef=None,
            title=operation.title,
            durationSlots=operation.duration_slots,
            segments=[],
            isPinned=False,
            isCompleted=False,
        )
        candidate_items.append(added_item)
        candidate_by_id[item_id] = added_item
        schedule_targets.append(
            _ScheduleTarget(
                item_id=item_id,
                placement=operation.placement,
                placement_is_explicit=operation.placement is not None,
                priority=operation.priority,
                input_order=operation.input_order,
                operation_index=operation_index,
            )
        )

    occupied = [False] * 96
    scheduled_ids = {target.item_id for target in schedule_targets}
    for item in candidate_items:
        if item.item_id not in scheduled_ids:
            _occupy(occupied, item.segments)

    schedule_targets.sort(
        key=lambda target: (
            not target.placement_is_explicit,
            target.priority is None,
            -(target.priority or 0),
            target.input_order,
            target.operation_index,
        )
    )
    earliest_slot = _first_available_slot(request.now)
    for target in schedule_targets:
        item = candidate_by_id[target.item_id]
        if target.placement is not None and (
            (target.placement.anchor == "start" and target.placement.slot == 96)
            or (target.placement.anchor == "end" and target.placement.slot == 0)
        ):
            segments: list[TimeFragmentSegmentV2] = []
            _add_issue(
                issues,
                code="INVALID_TIME",
                message="指定的时间锚点不在可排期范围内",
                item_id=item.item_id,
                field="placement.slot",
            )
        else:
            segments = _allocate_segments(
                occupied,
                item.duration_slots,
                placement=target.placement,
                earliest_slot=earliest_slot,
            )
        item = item.model_copy(update={"segments": segments}, deep=True)
        _replace_item(candidate_items, item)
        candidate_by_id[item.item_id] = item
        if segments:
            _occupy(occupied, segments)
        else:
            _add_issue(
                issues,
                code="UNPLACED",
                message=f"对象「{item.title}」在当天剩余空间中无法完整排下",
                severity="warning",
                item_id=item.item_id,
                field="segments",
            )

    proposal = TimeFragmentPlanProposal(
        baseFingerprint=request.base_fingerprint,
        algorithmVersion=TIME_FRAGMENT_PLANNER_VERSION,
        deletedOccurrenceIDs=deleted_occurrence_ids,
        deletedExternalEventIDs=deleted_external_event_ids,
        operations=normalized_operations,
        candidatePlan=TimeFragmentPlanV2(
            date=request.current_plan.date,
            items=candidate_items,
        ),
    )
    issues.extend(validate_time_fragment_proposal(request, proposal))
    issues = _deduplicate_issues(issues)
    validation = TimeFragmentValidation(
        valid=not any(issue.severity == "error" for issue in issues),
        attempts=attempts,
        issues=issues,
    )
    return TimeFragmentPlanResponseV2(
        requestID=request.request_id,
        proposal=proposal,
        validation=validation,
    )


def validate_time_fragment_proposal(
    request: TimeFragmentPlanRequestV2,
    proposal: TimeFragmentPlanProposal,
) -> list[TimeFragmentValidationIssue]:
    issues: list[TimeFragmentValidationIssue] = []
    base_items = request.current_plan.items
    candidate_items = proposal.candidate_plan.items
    base_counts = Counter(item.item_id for item in base_items)
    candidate_counts = Counter(item.item_id for item in candidate_items)
    base_index = {item.item_id: item for item in base_items if base_counts[item.item_id] == 1}
    candidate_index = {
        item.item_id: item for item in candidate_items if candidate_counts[item.item_id] == 1
    }

    local_date = datetime.fromisoformat(request.now.replace("Z", "+00:00")).date().isoformat()
    if proposal.base_fingerprint != request.base_fingerprint:
        _add_issue(
            issues,
            code="INVALID_OPERATION",
            message="proposal 没有原样回显 baseFingerprint",
            field="baseFingerprint",
        )
    if proposal.candidate_plan.date != request.current_plan.date or proposal.candidate_plan.date != local_date:
        _add_issue(
            issues,
            code="CROSS_DAY",
            message="candidatePlan 日期与请求本地自然日不一致",
            field="candidatePlan.date",
        )
    if any(count > 1 for count in candidate_counts.values()):
        _add_issue(
            issues,
            code="ID_SET_MISMATCH",
            message="candidatePlan 包含重复 itemId",
            field="candidatePlan.items",
        )

    add_operations = [
        operation for operation in proposal.operations if isinstance(operation, TimeFragmentAddOperation)
    ]
    delete_operations = [
        operation for operation in proposal.operations if isinstance(operation, TimeFragmentDeleteOperation)
    ]
    valid_deleted_occurrences: set[str] = set()
    valid_deleted_external_events: set[str] = set()
    valid_deleted_target_ids: set[str] = set()
    for operation in delete_operations:
        target = base_index.get(operation.target_item_id)
        if (
            target is None
            or operation.object_type is not None
            and operation.object_type != target.object_type
        ):
            continue
        valid_deleted_target_ids.add(target.item_id)
        if isinstance(target, TimeFragmentInternalTaskItem) and target.domain_ref is not None:
            valid_deleted_occurrences.add(target.item_id)
        elif isinstance(target, TimeFragmentExternalEventItem):
            valid_deleted_external_events.add(target.item_id)
    if (
        len(proposal.deleted_occurrence_ids) != len(set(proposal.deleted_occurrence_ids))
        or len(proposal.deleted_external_event_ids)
        != len(set(proposal.deleted_external_event_ids))
        or set(proposal.deleted_occurrence_ids) != valid_deleted_occurrences
        or set(proposal.deleted_external_event_ids) != valid_deleted_external_events
    ):
        _add_issue(
            issues,
            code="ID_SET_MISMATCH",
            message="显式删除集合与 delete operations 不一致",
            field="deletedOccurrenceIDs",
        )

    expected_ids = (
        set(base_counts)
        - valid_deleted_target_ids
    ) | {str(operation.temporary_id) for operation in add_operations}
    if set(candidate_counts) != expected_ids:
        _add_issue(
            issues,
            code="ID_SET_MISMATCH",
            message="candidatePlan ID 集合不等于基线减删除再加新增 ID",
            field="candidatePlan.items",
        )

    indexed_operations_by_target: dict[str, list[TimeFragmentOperation]] = {}
    allowed_by_target: dict[str, set[str]] = {}
    expected_duration_by_target: dict[str, int] = {}
    for operation in proposal.operations:
        if isinstance(operation, TimeFragmentAddOperation):
            continue
        indexed_operations_by_target.setdefault(operation.target_item_id, []).append(operation)
        target = base_index.get(operation.target_item_id)
        if target is None:
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作引用的 itemId 不存在于 currentPlan",
                item_id=operation.target_item_id,
                field="targetItemId",
            )
            continue
        if operation.object_type is not None and operation.object_type != target.object_type:
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作 objectType 与 currentPlan 目标不匹配",
                item_id=target.item_id,
                field="objectType",
            )
            continue
        allowed_by_target.setdefault(target.item_id, set()).update(operation.allowed_changes)
        if isinstance(operation, TimeFragmentChangeDurationOperation):
            expected_duration_by_target[target.item_id] = operation.duration_slots

    for item_id, operations in indexed_operations_by_target.items():
        operation_types = [operation.type for operation in operations]
        if len(operation_types) != len(set(operation_types)) or (
            "delete" in operation_types and len(operation_types) > 1
        ):
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标存在重复或互相矛盾的操作",
                item_id=item_id,
                field="operations",
            )

    for item_id, base_item in base_index.items():
        if item_id in valid_deleted_target_ids:
            continue
        candidate_item = candidate_index.get(item_id)
        if candidate_item is None:
            continue
        if type(candidate_item) is not type(base_item):
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="candidatePlan 改变了已有对象的 objectType",
                item_id=item_id,
                field="objectType",
            )
            continue
        allowed = allowed_by_target.get(item_id, set())
        before = base_item.model_dump(mode="json", by_alias=True)
        after = candidate_item.model_dump(mode="json", by_alias=True)
        for field_name in before:
            if before[field_name] != after[field_name] and field_name not in allowed:
                _add_issue(
                    issues,
                    code="PROTECTED_OBJECT",
                    message="candidatePlan 修改了 operation 未授权的字段",
                    item_id=item_id,
                    field=field_name,
                )
        expected_duration = expected_duration_by_target.get(item_id)
        if expected_duration is not None and candidate_item.duration_slots != expected_duration:
            _add_issue(
                issues,
                code="INVALID_DURATION",
                message="candidatePlan 时长与 changeDuration operation 不一致",
                item_id=item_id,
                field="durationSlots",
            )

    for operation in add_operations:
        item = candidate_index.get(str(operation.temporary_id))
        if not isinstance(item, TimeFragmentInternalTaskItem) or item.domain_ref is not None:
            _add_issue(
                issues,
                code="ID_SET_MISMATCH",
                message="新增项必须是 domainRef 为空的 internalTask",
                item_id=str(operation.temporary_id),
                field="domainRef",
            )
            continue
        if item.title != operation.title:
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="新增项标题与 add operation 不一致",
                item_id=item.item_id,
                field="title",
            )
        if item.duration_slots != operation.duration_slots:
            _add_issue(
                issues,
                code="INVALID_DURATION",
                message="新增项时长与 add operation 不一致",
                item_id=item.item_id,
                field="durationSlots",
            )
        if item.is_pinned or item.is_completed:
            _add_issue(
                issues,
                code="PROTECTED_OBJECT",
                message="新增项不能伪造只读事实",
                item_id=item.item_id,
                field="isPinned",
            )

    occupancy: dict[int, str] = {}
    for item in candidate_items:
        duration = sum(segment.end_slot - segment.start_slot for segment in item.segments)
        if item.segments and duration != item.duration_slots:
            _add_issue(
                issues,
                code="PARTIAL_PLACEMENT",
                message="对象时间片总时长与 durationSlots 不一致",
                item_id=item.item_id,
                field="segments",
            )
        for segment in item.segments:
            for slot in range(segment.start_slot, segment.end_slot):
                owner = occupancy.get(slot)
                if owner is not None:
                    _add_issue(
                        issues,
                        code="CONFLICT",
                        message="candidatePlan 中的时间片发生重叠",
                        item_id=item.item_id,
                        field="segments",
                    )
                else:
                    occupancy[slot] = item.item_id

    for operation in proposal.operations:
        if not isinstance(operation, (TimeFragmentAddOperation, TimeFragmentMoveOperation)):
            continue
        if operation.placement is None:
            continue
        item_id = (
            str(operation.temporary_id)
            if isinstance(operation, TimeFragmentAddOperation)
            else operation.target_item_id
        )
        item = candidate_index.get(item_id)
        if item is None or not item.segments:
            continue
        actual_slot = (
            item.segments[0].start_slot
            if operation.placement.anchor == "start"
            else item.segments[-1].end_slot
        )
        if actual_slot != operation.placement.slot:
            _add_issue(
                issues,
                code="INVALID_TIME",
                message="排期结果没有遵守用户指定的时间锚点",
                item_id=item_id,
                field="segments",
            )
    return _deduplicate_issues(issues)


def _normalize_operations(
    model_output: TimeFragmentModelOperations,
    base_index: dict[str, TimeFragmentPlanItem],
    *,
    existing_item_ids: set[str],
    uuid_factory: Callable[[], UUID],
) -> list[TimeFragmentOperation]:
    used_ids = set(existing_item_ids)
    normalized: list[TimeFragmentOperation] = []
    for operation in model_output.operations:
        operation_data = operation.model_dump(mode="json", by_alias=True)
        if isinstance(operation, TimeFragmentModelAddOperation):
            temporary_id = uuid_factory()
            while str(temporary_id) in used_ids:
                temporary_id = uuid_factory()
            used_ids.add(str(temporary_id))
            operation_data["temporaryId"] = temporary_id
            normalized.append(TimeFragmentAddOperation.model_validate(operation_data))
            continue
        target = base_index.get(operation.target_item_id)
        if operation.object_type is None and target is not None:
            operation_data["objectType"] = target.object_type
        operation_data.pop("authorizationText", None)
        operation_type = {
            "move": TimeFragmentMoveOperation,
            "changeDuration": TimeFragmentChangeDurationOperation,
            "delete": TimeFragmentDeleteOperation,
        }[operation.type]
        normalized.append(operation_type.model_validate(operation_data))
    return normalized


def _is_protected(item: TimeFragmentPlanItem) -> bool:
    if isinstance(item, TimeFragmentExternalEventItem):
        return True
    return item.is_pinned or item.is_completed


def _protected_operation_is_authorized(
    request_text: str,
    item: TimeFragmentPlanItem,
    operation: TimeFragmentOperation,
    authorization_text: str | None,
) -> bool:
    if authorization_text is None:
        return False
    evidence = authorization_text.strip()
    if not evidence or evidence not in request_text:
        return False
    clause = _clause_containing(request_text, evidence)
    if any(marker in clause for marker in ("不要", "别", "无需", "不许", "不能", "禁止", "保持不变", "保持原样")):
        return False
    intent_terms = {
        "move": ("移动", "移到", "挪到", "改到", "调到", "重排", "安排", "调整"),
        "changeDuration": ("时长", "延长", "缩短", "改成", "调整时长", "修改时长"),
        "delete": ("删除", "删掉", "移除", "取消", "不要了"),
    }[operation.type]
    if not any(term in evidence for term in intent_terms):
        return False
    if item.title in evidence or item.item_id in evidence:
        return True
    return _evidence_scope_contains_item(evidence, item)


def _clause_containing(text: str, evidence: str) -> str:
    evidence_start = text.find(evidence)
    delimiters = "，,。；;！!？?\n"
    clause_start = max((text.rfind(delimiter, 0, evidence_start) for delimiter in delimiters), default=-1)
    clause_ends = [
        index
        for delimiter in delimiters
        if (index := text.find(delimiter, evidence_start + len(evidence))) != -1
    ]
    clause_end = min(clause_ends, default=len(text))
    return text[clause_start + 1 : clause_end]


def _evidence_scope_contains_item(evidence: str, item: TimeFragmentPlanItem) -> bool:
    if any(scope in evidence for scope in ("今天", "全天", "整天", "全部", "所有")):
        return True
    slot_ranges = {
        "上午": (0, 48),
        "中午": (44, 56),
        "下午": (48, 72),
        "傍晚": (68, 80),
        "晚上": (72, 96),
    }
    matching_ranges = [bounds for scope, bounds in slot_ranges.items() if scope in evidence]
    if not matching_ranges or not item.segments:
        return False
    return any(
        all(
            start_slot <= segment.start_slot and segment.end_slot <= end_slot
            for segment in item.segments
        )
        for start_slot, end_slot in matching_ranges
    )


def _first_available_slot(now: str) -> int:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    slot = parsed.hour * 4 + parsed.minute // 15
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        slot += 1
    return min(slot, 96)


def _allocate_segments(
    occupied: list[bool],
    duration_slots: int,
    *,
    placement: TimeFragmentPlacement | None,
    earliest_slot: int,
) -> list[TimeFragmentSegmentV2]:
    if placement is None or placement.anchor == "start":
        start = placement.slot if placement is not None else earliest_slot
        if start >= 96 or occupied[start]:
            return []
        candidates = range(start, 96)
    else:
        end = placement.slot
        if end <= 0 or occupied[end - 1]:
            return []
        candidates = range(end - 1, -1, -1)

    selected: list[int] = []
    for slot in candidates:
        if not occupied[slot]:
            selected.append(slot)
            if len(selected) == duration_slots:
                break
    if len(selected) != duration_slots:
        return []
    selected.sort()
    return _slots_to_segments(selected)


def _slots_to_segments(slots: list[int]) -> list[TimeFragmentSegmentV2]:
    segments: list[TimeFragmentSegmentV2] = []
    start = previous = slots[0]
    for slot in slots[1:]:
        if slot != previous + 1:
            segments.append(TimeFragmentSegmentV2(startSlot=start, endSlot=previous + 1))
            start = slot
        previous = slot
    segments.append(TimeFragmentSegmentV2(startSlot=start, endSlot=previous + 1))
    return segments


def _occupy(occupied: list[bool], segments: list[TimeFragmentSegmentV2]) -> None:
    for segment in segments:
        for slot in range(segment.start_slot, segment.end_slot):
            occupied[slot] = True


def _replace_item(items: list[TimeFragmentPlanItem], replacement: TimeFragmentPlanItem) -> None:
    for index, item in enumerate(items):
        if item.item_id == replacement.item_id:
            items[index] = replacement
            return


def _add_issue(
    issues: list[TimeFragmentValidationIssue],
    *,
    code: str,
    message: str,
    severity: Literal["error", "warning"] = "error",
    item_id: str | None = None,
    field: str | None = None,
) -> None:
    issues.append(
        TimeFragmentValidationIssue(
            source="model_server",
            severity=severity,
            code=code,
            message=message,
            itemId=item_id,
            field=field,
        )
    )


def _deduplicate_issues(
    issues: list[TimeFragmentValidationIssue],
) -> list[TimeFragmentValidationIssue]:
    seen: set[tuple[str, str, str | None, str | None]] = set()
    result: list[TimeFragmentValidationIssue] = []
    for issue in issues:
        key = (issue.severity, issue.code, issue.item_id, issue.field)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
