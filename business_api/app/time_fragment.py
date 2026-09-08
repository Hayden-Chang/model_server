import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from .contracts import (
    TimeFragmentAddOperation,
    TimeFragmentChangeDurationOperation,
    TimeFragmentChangeTitleOperation,
    TimeFragmentDeleteOperation,
    TimeFragmentExternalEventItem,
    TimeFragmentExtractedOperations,
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
from .time_fragment_solver import SlotAllocationRelation, SlotAllocationTarget, relations_hold, solve_slot_allocations


TIME_FRAGMENT_PLANNER_VERSION = "time-fragment-planner-v1"
_TEMPORARY_ID_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://timefragment.app/time-fragment-plan-v2/temporary-id",
)
_EXACT_CLOCK_PATTERN = re.compile(
    r"(?<!\d)(?:(?:[01]?\d|2[0-3])[:：][0-5]\d|24[:：]00)(?!\d)"
    r"|(?<![零〇一二两三四五六七八九十\d])"
    r"(?:二十[一二三四]?|十[一二三四五六七八九]?|[零〇一二两三四五六七八九]"
    r"|(?:[01]?\d|2[0-3]|24))\s*点"
)
_CLOCK_PERIODS = (
    "上午",
    "中午",
    "下午",
    "晚上",
    "傍晚",
    "凌晨",
    "早上",
    "清晨",
)
_CLOCK_NUMBER_SOURCE = (
    r"(?:二十[一二三四]?|十[一二三四五六七八九]?|[零〇一二两三四五六七八九]"
    r"|(?:[01]?\d|2[0-4]))"
)
_CLOCK_TOKEN_SOURCE = (
    rf"(?:(?:{'|'.join(_CLOCK_PERIODS)})\s*)?"
    rf"(?:{_CLOCK_NUMBER_SOURCE}\s*点"
    rf"(?:\s*(?:半|一刻|三刻|[零〇一二两三四五六七八九十\d]{{1,3}})\s*分?)?"
    r"|(?:[01]?\d|2[0-3]|24)[:：][0-5]\d)"
)
_CLOCK_TOKEN_PATTERN = re.compile(_CLOCK_TOKEN_SOURCE)
_CLOCK_RANGE_PATTERN = re.compile(
    rf"(?P<start>{_CLOCK_TOKEN_SOURCE})\s*\\?(?:到|至|[-–—~～])\s*"
    rf"(?P<end>{_CLOCK_TOKEN_SOURCE})"
)
_CONTINUOUS_TIMEPOINT_SEPARATOR_PATTERN = re.compile(r"[，,。；;！？!?\n]+")
_CONTINUOUS_TIMEPOINT_END_MARKERS = {
    "出地铁": "坐地铁",
    "到家": "下班",
}
_CONTINUOUS_TIMEPOINT_COMBINED_TITLES = {
    ("下班", "到家"): "下班回家",
}


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
        earliestStartSlot=request.earliest_start_slot,
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
    enforces_earliest_start: bool
    cascaded: bool = False


def plan_time_fragment(
    request: TimeFragmentPlanRequestV2,
    model_output: TimeFragmentModelOperations | TimeFragmentExtractedOperations,
    *,
    attempts: Literal[1, 2] = 1,
    uuid_factory: Callable[[], UUID] | None = None,
) -> TimeFragmentPlanResponseV2:
    """Expand parsed model operations into a complete, deterministic candidate plan.

    Semantic failures are represented as validation issues while retaining the
    candidate. Structural model-output failures are handled by WP3 with
    ``build_time_fragment_parse_failed_response``.
    """

    extracted_clocks = isinstance(model_output, TimeFragmentExtractedOperations)
    clock_errors: dict[int, str] = {}
    relation_indexes: list[tuple[int, int, bool]] = []
    relation_errors: list[str] = []
    if extracted_clocks:
        from .time_fragment_clocks import compile_time_fragment_clocks
        from .time_fragment_relations import compile_temporal_relations

        extracted_output = model_output
        model_output, clock_errors = compile_time_fragment_clocks(
            model_output,
            request.text,
            existing_items=request.current_plan.items,
            earliest_start_slot=request.earliest_start_slot,
        )
        model_output, relation_indexes, relation_errors = compile_temporal_relations(
            extracted_output, model_output, request.text,
        )
    continuous_timepoint_adds = (
        None if extracted_clocks else _continuous_timepoint_add_operations(request.text)
    )
    if (
        continuous_timepoint_adds is not None
        and model_output.operations
        and all(
            isinstance(operation, TimeFragmentModelAddOperation)
            for operation in model_output.operations
        )
    ):
        model_output = TimeFragmentModelOperations(operations=continuous_timepoint_adds)

    base_items = [item.model_copy(deep=True) for item in request.current_plan.items]
    base_counts = Counter(item.item_id for item in base_items)
    base_index = {item.item_id: item for item in base_items if base_counts[item.item_id] == 1}
    normalized_operations = _normalize_operations(
        model_output,
        base_index,
        request_text=request.text,
        request_id=request.request_id,
        existing_item_ids=set(base_counts),
        uuid_factory=uuid_factory,
        extracted_clocks=extracted_clocks,
    )
    normalized_operations = _apply_clear_after_insertion_intent(
        request.text,
        base_items,
        normalized_operations,
    )
    relations = [
        SlotAllocationRelation(str(normalized_operations[before].temporary_id),
                               str(normalized_operations[after].temporary_id), adjacent)
        for before, after, adjacent in relation_indexes
    ]
    has_clear_after_insertion = any(
        isinstance(operation, TimeFragmentAddOperation)
        and _clear_after_insertion_slot(request.text, operation.title, base_items) is not None
        for operation in normalized_operations
    )
    issues: list[TimeFragmentValidationIssue] = []
    for message in relation_errors:
        _add_issue(issues, code="INVALID_OPERATION", message=message, field="temporalRelations")
    invalid_clock_ids: set[str] = set()
    for index, message in clock_errors.items():
        item_id = None
        if index >= 0:
            item_id = str(normalized_operations[index].temporary_id)
            invalid_clock_ids.add(item_id)
        _add_issue(
            issues, code="INVALID_OPERATION", message=message,
            item_id=item_id, field="timeConstraint",
        )
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
    model_derived_move_indexes: set[int] = set()
    for index, (operation, model_operation) in enumerate(
        zip(normalized_operations, model_output.operations, strict=True)
    ):
        if not isinstance(operation, TimeFragmentAddOperation):
            target = base_index.get(operation.target_item_id)
            if (
                has_clear_after_insertion
                and isinstance(operation, TimeFragmentMoveOperation)
                and isinstance(target, TimeFragmentInternalTaskItem)
                and not _is_protected(target)
                and not _operation_is_explicitly_requested(
                    request.text,
                    target,
                    operation,
                    getattr(model_operation, "authorization_text", None),
                )
            ):
                model_derived_move_indexes.add(index)
                continue
            operations_by_target.setdefault(operation.target_item_id, []).append(
                (index, operation, model_operation)
            )

    invalid_targets: set[str] = set()
    for item_id, indexed_operations in operations_by_target.items():
        target = base_index.get(item_id)
        if target is None:
            invalid_targets.add(item_id)
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作引用的 itemId 不存在于 currentPlan",
                item_id=item_id,
                field="targetItemId",
            )
            continue
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
        priorities = {
            operation.priority
            for _, operation, _ in indexed_operations
            if getattr(operation, "priority", None) is not None
        }
        if len(priorities) > 1:
            invalid_targets.add(item_id)
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标的操作包含冲突的 priority",
                item_id=item_id,
                field="priority",
            )
        if item_id in invalid_targets:
            continue
        if any(
            operation.object_type is not None and operation.object_type != target.object_type
            for _, operation, _ in indexed_operations
        ):
            invalid_targets.add(item_id)
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作 objectType 与 currentPlan 目标不匹配",
                item_id=item_id,
                field="objectType",
            )

    unauthorized_protected_targets: set[str] = set()
    for item_id, indexed_operations in operations_by_target.items():
        if item_id in invalid_targets:
            continue
        target = base_index[item_id]
        if _is_protected(target) and any(
            not _protected_operation_is_authorized(
                request.text,
                target,
                operation,
                getattr(model_operation, "authorization_text", None),
            )
            for _, operation, model_operation in indexed_operations
        ):
            unauthorized_protected_targets.add(item_id)
            _add_issue(
                issues,
                code="PROTECTED_OBJECT",
                message=f"用户没有明确授权修改受保护对象「{target.title}」",
                item_id=item_id,
                field="operations",
            )

    normalized_operations = [
        operation
        for index, operation in enumerate(normalized_operations)
        if index not in model_derived_move_indexes
        and (
            isinstance(operation, TimeFragmentAddOperation)
            or operation.target_item_id not in unauthorized_protected_targets
        )
    ]

    candidate_items = [item.model_copy(deep=True) for item in base_items]
    candidate_by_id = {item.item_id: item for item in candidate_items if base_counts[item.item_id] == 1}
    candidate_deleted_ids: set[str] = set()
    deleted_occurrence_ids: list[str] = []
    deleted_external_event_ids: list[str] = []
    schedule_targets: list[_ScheduleTarget] = []

    for item_id, indexed_operations in operations_by_target.items():
        if item_id in invalid_targets:
            continue
        if item_id in unauthorized_protected_targets:
            continue
        target = base_index[item_id]
        operations = [operation for _, operation, _ in indexed_operations]
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
        priorities = {
            operation.priority
            for operation in operations
            if getattr(operation, "priority", None) is not None
        }
        for operation in operations:
            if isinstance(operation, TimeFragmentChangeDurationOperation):
                candidate = candidate.model_copy(
                    update={"duration_slots": operation.duration_slots},
                    deep=True,
                )
            elif isinstance(operation, TimeFragmentMoveOperation):
                placement = operation.placement
            elif isinstance(operation, TimeFragmentChangeTitleOperation):
                candidate = candidate.model_copy(
                    update={"title": operation.title},
                    deep=True,
                )
        requires_scheduling = any(
            isinstance(
                operation,
                (TimeFragmentMoveOperation, TimeFragmentChangeDurationOperation),
            )
            for operation in operations
        )
        if not requires_scheduling:
            _replace_item(candidate_items, candidate)
            candidate_by_id[item_id] = candidate
            continue
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
                enforces_earliest_start=any(
                    isinstance(operation, TimeFragmentMoveOperation)
                    for operation in operations
                ),
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
        if item_id in invalid_clock_ids:
            continue
        schedule_targets.append(
            _ScheduleTarget(
                item_id=item_id,
                placement=operation.placement,
                placement_is_explicit=operation.placement is not None,
                priority=operation.priority,
                input_order=operation.input_order,
                operation_index=operation_index,
                enforces_earliest_start=True,
            )
        )

    added_ids = {
        str(operation.temporary_id)
        for operation in normalized_operations
        if isinstance(operation, TimeFragmentAddOperation)
    }
    insertion_anchors = [
        target.placement.slot
        for target in schedule_targets
        if target.item_id in added_ids
        and target.placement is not None
        and target.placement.anchor == "start"
    ]
    if insertion_anchors:
        cascade_start = min(insertion_anchors)
        already_scheduled = {target.item_id for target in schedule_targets}
        cascading_items = sorted(
            (
                item
                for item in base_items
                if isinstance(item, TimeFragmentInternalTaskItem)
                and not _is_protected(item)
                and item.item_id not in already_scheduled
                and item.item_id not in candidate_deleted_ids
                and item.segments
                and any(segment.end_slot > cascade_start for segment in item.segments)
            ),
            key=lambda item: min(segment.start_slot for segment in item.segments),
        )
        next_input_order = max(
            (target.input_order for target in schedule_targets),
            default=-1,
        ) + 1
        for offset, item in enumerate(cascading_items):
            candidate = candidate_by_id[item.item_id].model_copy(
                update={"segments": []},
                deep=True,
            )
            _replace_item(candidate_items, candidate)
            candidate_by_id[item.item_id] = candidate
            schedule_targets.append(
                _ScheduleTarget(
                    item_id=item.item_id,
                    placement=TimeFragmentPlacement(
                        anchor="start",
                        slot=min(segment.start_slot for segment in item.segments),
                    ),
                    placement_is_explicit=False,
                    priority=None,
                    input_order=next_input_order + offset,
                    operation_index=len(normalized_operations) + offset,
                    enforces_earliest_start=False,
                    cascaded=True,
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
    earliest_slot = _first_available_slot(request)
    current_day_slot = _current_day_slot(request)
    invalid_target_ids: set[str] = set()
    day_end_ids: set[str] = set()
    solver_targets: list[SlotAllocationTarget] = []
    for target in schedule_targets:
        item = candidate_by_id[target.item_id]
        target_earliest_slot = earliest_slot if target.enforces_earliest_start else 0
        placement_precedes_earliest = (
            target.placement is not None
            and target.placement_is_explicit
            and _placement_precedes_slot(
                target.placement,
                item.duration_slots,
                target_earliest_slot,
            )
        )
        placement_precedes_current_time = (
            target.placement is not None
            and target.placement_is_explicit
            and current_day_slot is not None
            and _placement_precedes_slot(
                target.placement,
                item.duration_slots,
                current_day_slot,
            )
        )
        placement_outside_day = target.placement is not None and (
            (target.placement.anchor == "start" and target.placement.slot == 96)
            or (target.placement.anchor == "end" and target.placement.slot == 0)
        )
        midnight_unplaced_add = (
            target.item_id in added_ids
            and target.placement is not None
            and target.placement.anchor == "start"
            and target.placement.slot == 96
        )
        if midnight_unplaced_add:
            day_end_ids.add(target.item_id)
        if placement_outside_day or placement_precedes_earliest:
            invalid_target_ids.add(target.item_id)
            _add_issue(
                issues,
                code="INVALID_TIME",
                message=(
                    "指定时间已到当天结束（24:00），已保留为未排任务"
                    if midnight_unplaced_add
                    else "指定时间早于当前可排期起点，已保留为未排任务"
                    if placement_precedes_current_time
                    else "指定的时间锚点不在可排期范围内"
                ),
                severity=(
                    "warning" if midnight_unplaced_add or placement_precedes_current_time else "error"
                ),
                item_id=item.item_id,
                field="placement.slot",
            )
            continue
        solver_targets.append(
            SlotAllocationTarget(
                item_id=target.item_id,
                duration_slots=item.duration_slots,
                earliest_slot=target_earliest_slot,
                anchor=target.placement.anchor if target.placement is not None else None,
                anchor_slot=target.placement.slot if target.placement is not None else None,
                require_anchor=not target.cascaded,
            )
        )

    active_relations = [relation for relation in relations if relation.after_id not in day_end_ids]
    solved_slots = (
        solve_slot_allocations(occupied, solver_targets, active_relations)
        if active_relations else solve_slot_allocations(occupied, solver_targets)
    )
    if active_relations and solved_slots is None:
        _add_issue(issues, code="INVALID_OPERATION", message="未能在时间预算内求出满足先后关系的方案，请重新调整", field="temporalRelations")
    for target in schedule_targets:
        item = candidate_by_id[target.item_id]
        target_earliest_slot = earliest_slot if target.enforces_earliest_start else 0
        if target.item_id in invalid_target_ids:
            segments: list[TimeFragmentSegmentV2] = []
        elif solved_slots is None:
            segments = _allocate_segments(
                occupied,
                item.duration_slots,
                placement=target.placement,
                earliest_slot=target_earliest_slot,
                allow_occupied_anchor=target.cascaded,
            )
        else:
            selected_slots = solved_slots.get(target.item_id, [])
            segments = _slots_to_segments(selected_slots) if selected_slots else []
        item = item.model_copy(update={"segments": segments}, deep=True)
        _replace_item(candidate_items, item)
        candidate_by_id[item.item_id] = item
        if target.cascaded and item.segments != base_index[item.item_id].segments:
            normalized_operations.append(
                TimeFragmentMoveOperation(
                    type="move",
                    targetItemId=item.item_id,
                    objectType="internalTask",
                    allowedChanges=["segments"],
                    placement=(
                        TimeFragmentPlacement(
                            anchor="start",
                            slot=item.segments[0].start_slot,
                        )
                        if item.segments
                        else None
                    ),
                    inputOrder=target.input_order,
                )
            )
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
    assignments = {
        item.item_id: [slot for segment in item.segments for slot in range(segment.start_slot, segment.end_slot)]
        for item in candidate_items
    }
    # A 24:00 add stays a Todo, but its known boundary still ends the preceding day.
    assignments.update({item_id: [96] for item_id in day_end_ids
                        if any(relation.after_id == item_id and assignments.get(relation.before_id) for relation in relations)})
    if not relations_hold(assignments, relations):
        _add_issue(issues, code="INVALID_OPERATION", message="方案没有满足活动之间的先后或连续关系", field="temporalRelations")
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

    if proposal.base_fingerprint != request.base_fingerprint:
        _add_issue(
            issues,
            code="INVALID_OPERATION",
            message="proposal 没有原样回显 baseFingerprint",
            field="baseFingerprint",
        )
    if proposal.candidate_plan.date != request.current_plan.date:
        _add_issue(
            issues,
            code="CROSS_DAY",
            message="candidatePlan 日期与 currentPlan 日期不一致",
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
    indexed_operations_by_target: dict[str, list[TimeFragmentOperation]] = {}
    for operation in proposal.operations:
        if not isinstance(operation, TimeFragmentAddOperation):
            indexed_operations_by_target.setdefault(operation.target_item_id, []).append(operation)

    invalid_operation_targets: set[str] = set()
    for item_id, operations in indexed_operations_by_target.items():
        target = base_index.get(item_id)
        if target is None:
            invalid_operation_targets.add(item_id)
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作引用的 itemId 不存在于 currentPlan",
                item_id=item_id,
                field="targetItemId",
            )
            continue
        operation_types = [operation.type for operation in operations]
        if len(operation_types) != len(set(operation_types)) or (
            "delete" in operation_types and len(operation_types) > 1
        ):
            invalid_operation_targets.add(item_id)
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标存在重复或互相矛盾的操作",
                item_id=item_id,
                field="operations",
            )
        priorities = {
            operation.priority
            for operation in operations
            if getattr(operation, "priority", None) is not None
        }
        if len(priorities) > 1:
            invalid_operation_targets.add(item_id)
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="同一目标的操作包含冲突的 priority",
                item_id=item_id,
                field="priority",
            )
        if item_id in invalid_operation_targets:
            continue
        if any(
            operation.object_type is not None and operation.object_type != target.object_type
            for operation in operations
        ):
            invalid_operation_targets.add(item_id)
            _add_issue(
                issues,
                code="UNKNOWN_TARGET",
                message="操作 objectType 与 currentPlan 目标不匹配",
                item_id=item_id,
                field="objectType",
            )

    valid_deleted_occurrences: set[str] = set()
    valid_deleted_external_events: set[str] = set()
    valid_deleted_target_ids: set[str] = set()
    for item_id, operations in indexed_operations_by_target.items():
        if item_id in invalid_operation_targets or not isinstance(
            operations[0],
            TimeFragmentDeleteOperation,
        ):
            continue
        target = base_index[item_id]
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

    allowed_by_target: dict[str, set[str]] = {}
    expected_duration_by_target: dict[str, int] = {}
    expected_title_by_target: dict[str, str] = {}
    for item_id, operations in indexed_operations_by_target.items():
        if item_id in invalid_operation_targets:
            continue
        target = base_index[item_id]
        for operation in operations:
            allowed_by_target.setdefault(target.item_id, set()).update(operation.allowed_changes)
            if isinstance(operation, TimeFragmentChangeDurationOperation):
                expected_duration_by_target[target.item_id] = operation.duration_slots
            elif isinstance(operation, TimeFragmentChangeTitleOperation):
                expected_title_by_target[target.item_id] = operation.title

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
        expected_title = expected_title_by_target.get(item_id)
        if expected_title is not None and candidate_item.title != expected_title:
            _add_issue(
                issues,
                code="INVALID_OPERATION",
                message="candidatePlan 标题与 changeTitle operation 不一致",
                item_id=item_id,
                field="title",
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
        if isinstance(operation, TimeFragmentMoveOperation):
            target = base_index.get(operation.target_item_id)
            if (
                target is None
                or operation.target_item_id in invalid_operation_targets
                or operation.object_type is not None
                and operation.object_type != target.object_type
            ):
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
    request_text: str,
    request_id: str,
    existing_item_ids: set[str],
    uuid_factory: Callable[[], UUID] | None,
    extracted_clocks: bool = False,
) -> list[TimeFragmentOperation]:
    used_ids = set(existing_item_ids)
    normalized: list[TimeFragmentOperation] = []
    requested_add_priorities = _requested_add_priority_scores(
        request_text,
        model_output.operations,
    )
    requested_add_timings = {} if extracted_clocks else _requested_add_time_constraints(
        request_text,
        model_output.operations,
    )
    for operation_index, operation in enumerate(model_output.operations):
        operation_data = operation.model_dump(mode="json", by_alias=True)
        if isinstance(operation, TimeFragmentModelAddOperation):
            operation_data.pop("authorizationText", None)
            title_label = _priority_label_in_model_title(request_text, operation.title)
            if title_label is not None:
                operation_data["title"] = title_label[0]
            if operation_index in requested_add_priorities:
                operation_data["priority"] = requested_add_priorities[operation_index]
            requested_timing = requested_add_timings.get(operation_index)
            if requested_timing is not None:
                placement, duration_slots = requested_timing
                operation_data["placement"] = placement
                if duration_slots is not None:
                    operation_data["durationSlots"] = duration_slots
            elif (
                not extracted_clocks
                and operation.placement is not None
                and not _add_placement_is_authorized(
                    request_text,
                    operation_data["title"],
                    operation.authorization_text,
                )
            ):
                operation_data["placement"] = None
            collision_index = 0
            temporary_id = _next_temporary_id(
                request_id=request_id,
                operation_index=operation_index,
                collision_index=collision_index,
                uuid_factory=uuid_factory,
            )
            while str(temporary_id) in used_ids:
                collision_index += 1
                temporary_id = _next_temporary_id(
                    request_id=request_id,
                    operation_index=operation_index,
                    collision_index=collision_index,
                    uuid_factory=uuid_factory,
                )
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
            "changeTitle": TimeFragmentChangeTitleOperation,
            "delete": TimeFragmentDeleteOperation,
        }[operation.type]
        normalized.append(operation_type.model_validate(operation_data))
    return normalized


def _requested_add_time_constraints(
    request_text: str,
    operations: list[TimeFragmentModelOperation],
) -> dict[int, tuple[TimeFragmentPlacement, int | None]]:
    add_operations = [
        (index, operation)
        for index, operation in enumerate(operations)
        if isinstance(operation, TimeFragmentModelAddOperation)
    ]
    continuous_timepoint_adds = _continuous_timepoint_add_operations(request_text)
    if (
        continuous_timepoint_adds is not None
        and len(add_operations) == len(operations) == len(continuous_timepoint_adds)
        and all(
            operation.title == requested_operation.title
            and operation.input_order == requested_operation.input_order
            for (_, operation), requested_operation in zip(
                add_operations,
                continuous_timepoint_adds,
                strict=True,
            )
        )
    ):
        return {
            operation_index: (
                requested_operation.placement,
                requested_operation.duration_slots,
            )
            for (operation_index, _), requested_operation in zip(
                add_operations,
                continuous_timepoint_adds,
                strict=True,
            )
            if requested_operation.placement is not None
        }

    constraints: dict[int, tuple[TimeFragmentPlacement, int | None]] = {}
    previous_range_end: int | None = None

    range_matches = list(_CLOCK_RANGE_PATTERN.finditer(request_text))
    for range_index, range_match in enumerate(range_matches):
        parsed_range = _parse_clock_range(range_match, previous_range_end)
        if parsed_range is None:
            continue
        previous_range_end = parsed_range[1]

        window_end = (
            range_matches[range_index + 1].start()
            if range_index + 1 < len(range_matches)
            else len(request_text)
        )
        title_window = request_text[range_match.end() : window_end]
        matching_operations = []
        for operation_index, operation in add_operations:
            title_label = _priority_label_in_model_title(request_text, operation.title)
            title = title_label[0] if title_label is not None else operation.title
            if title in title_window:
                matching_operations.append((operation_index, title))
        if len(matching_operations) != 1:
            continue

        operation_index, title = matching_operations[0]
        clause_start = max(
            request_text.rfind(separator, 0, range_match.start())
            for separator in ("，", ",", "。", "；", ";", "！", "!", "？", "?", "\n")
        ) + 1
        title_end = range_match.end() + title_window.index(title) + len(title)
        evidence = request_text[clause_start:title_end]
        if any(marker in evidence for marker in ("不要", "别", "无需", "不许", "不能", "禁止")):
            continue

        start_minutes, end_minutes = parsed_range
        if start_minutes % 15 == 0 and end_minutes % 15 == 0:
            constraints[operation_index] = (
                TimeFragmentPlacement(anchor="start", slot=start_minutes // 15),
                (end_minutes - start_minutes) // 15,
            )

    for line in request_text.splitlines():
        if _CLOCK_RANGE_PATTERN.search(line) is not None:
            continue

        matching_operations = []
        for operation_index, operation in add_operations:
            title_label = _priority_label_in_model_title(request_text, operation.title)
            title = title_label[0] if title_label is not None else operation.title
            if title in line:
                matching_operations.append((operation_index, operation))
        if len(matching_operations) != 1 or any(
            marker in line for marker in ("不要", "别", "无需", "不许", "不能", "禁止")
        ):
            continue

        operation_index, operation = matching_operations[0]
        token_match = _CLOCK_TOKEN_PATTERN.search(line)
        if token_match is None or operation.placement is None:
            continue
        parsed_clock = _parse_clock_token(token_match.group(0))
        if parsed_clock is None or parsed_clock[0] % 15 != 0:
            continue
        if operation.placement.slot == parsed_clock[0] // 15:
            constraints[operation_index] = (operation.placement, None)

    return constraints


def _continuous_timepoint_add_operations(
    request_text: str,
) -> list[TimeFragmentModelAddOperation] | None:
    if _CLOCK_RANGE_PATTERN.search(request_text) or any(
        marker in request_text for marker in ("不要", "别", "无需", "不许", "不能", "禁止")
    ):
        return None

    clauses = [
        clause.strip()
        for clause in _CONTINUOUS_TIMEPOINT_SEPARATOR_PATTERN.split(request_text)
        if clause.strip()
    ]
    if len(clauses) < 3:
        return None

    points: list[tuple[int, str]] = []
    previous_minutes: int | None = None
    for clause in clauses:
        token_matches = list(_CLOCK_TOKEN_PATTERN.finditer(clause))
        if len(token_matches) != 1 or token_matches[0].start() != 0:
            return None
        token_match = token_matches[0]
        action = clause[token_match.end() :].strip()
        parsed_clock = _parse_clock_token(token_match.group(0))
        if not action or parsed_clock is None:
            return None

        minutes, period = parsed_clock
        if previous_minutes is not None:
            if period is None and minutes <= previous_minutes:
                minutes += 12 * 60
            if minutes <= previous_minutes:
                return None
        if not 0 <= minutes <= 24 * 60:
            return None
        points.append((minutes, action))
        previous_minutes = minutes

    if points[-1][1] not in _CONTINUOUS_TIMEPOINT_END_MARKERS:
        return None
    for point_index, (_, action) in enumerate(points):
        expected_previous_action = _CONTINUOUS_TIMEPOINT_END_MARKERS.get(action)
        if expected_previous_action is not None and (
            point_index == 0 or points[point_index - 1][1] != expected_previous_action
        ):
            return None

    operations: list[TimeFragmentModelAddOperation] = []
    previous_end_slot: int | None = None
    for point_index, (start_minutes, action) in enumerate(points[:-1]):
        if action in _CONTINUOUS_TIMEPOINT_END_MARKERS:
            continue
        end_minutes, next_action = points[point_index + 1]
        if action == "吃饭":
            end_minutes = min(end_minutes, start_minutes + 30)
        start_slot = _round_minutes_to_slot(start_minutes)
        end_slot = _round_minutes_to_slot(end_minutes)
        if (
            start_slot >= end_slot
            or end_slot > 96
            or (
                previous_end_slot is not None
                and start_slot < previous_end_slot
            )
        ):
            return None
        title = _CONTINUOUS_TIMEPOINT_COMBINED_TITLES.get(
            (action, next_action),
            action,
        )
        operations.append(TimeFragmentModelAddOperation(
            type="add",
            title=title,
            duration_slots=end_slot - start_slot,
            placement=TimeFragmentPlacement(anchor="start", slot=start_slot),
            input_order=len(operations),
        ))
        previous_end_slot = end_slot

    return operations or None


def _round_minutes_to_slot(minutes: int) -> int:
    return (minutes + 7) // 15


def _parse_clock_range(
    match: re.Match[str],
    previous_range_end: int | None,
) -> tuple[int, int] | None:
    start = _parse_clock_token(match.group("start"))
    if start is None:
        return None
    start_minutes, start_period = start
    end = _parse_clock_token(match.group("end"), inherited_period=start_period)
    if end is None:
        return None
    end_minutes, end_period = end

    if (
        start_period is None
        and previous_range_end is not None
        and start_minutes < previous_range_end
        and start_minutes + 12 * 60 < 24 * 60
    ):
        start_minutes += 12 * 60
        if end_period is None and end_minutes < 12 * 60:
            end_minutes += 12 * 60
    if (
        end_minutes <= start_minutes
        and end_period is None
        and end_minutes + 12 * 60 <= 24 * 60
    ):
        end_minutes += 12 * 60
    if not 0 <= start_minutes < end_minutes <= 24 * 60:
        return None
    return start_minutes, end_minutes


def _parse_clock_token(
    token: str,
    *,
    inherited_period: str | None = None,
) -> tuple[int, str | None] | None:
    normalized = re.sub(r"\s+", "", token)
    period = next((value for value in _CLOCK_PERIODS if normalized.startswith(value)), None)
    if period is not None:
        normalized = normalized[len(period) :]
    effective_period = period or inherited_period

    if ":" in normalized or "：" in normalized:
        hour_text, minute_text = re.split("[:：]", normalized, maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    elif "点" in normalized:
        hour_text, minute_text = normalized.split("点", maxsplit=1)
        hour = _parse_chinese_clock_number(hour_text)
        if hour is None:
            return None
        minute_text = minute_text.removesuffix("分")
        if not minute_text:
            minute = 0
        elif minute_text == "半":
            minute = 30
        elif minute_text == "一刻":
            minute = 15
        elif minute_text == "三刻":
            minute = 45
        else:
            parsed_minute = _parse_chinese_clock_number(minute_text)
            if parsed_minute is None:
                return None
            minute = parsed_minute
    else:
        return None

    if not 0 <= hour <= 24 or not 0 <= minute < 60 or hour == 24 and minute != 0:
        return None
    if effective_period in {"下午", "晚上", "傍晚"} and 1 <= hour < 12:
        hour += 12
    elif effective_period == "中午" and 1 <= hour <= 5:
        hour += 12
    elif effective_period in {"凌晨", "上午", "早上", "清晨"} and hour == 12:
        hour = 0
    return hour * 60 + minute, period


def _parse_chinese_clock_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if "十" in value:
        tens_text, ones_text = value.split("十", maxsplit=1)
        tens = 1 if not tens_text else digits.get(tens_text)
        ones = 0 if not ones_text else digits.get(ones_text)
        return None if tens is None or ones is None else tens * 10 + ones
    parsed_digits = [digits.get(character) for character in value]
    if not parsed_digits or any(digit is None for digit in parsed_digits):
        return None
    return int("".join(str(digit) for digit in parsed_digits))


def _requested_add_priority_scores(
    request_text: str,
    operations: list[TimeFragmentModelOperation],
) -> dict[int, int]:
    add_operation_count = sum(
        isinstance(operation, TimeFragmentModelAddOperation)
        for operation in operations
    )
    labeled_operations: list[tuple[int, str, int]] = []
    for operation_index, operation in enumerate(operations):
        if not isinstance(operation, TimeFragmentModelAddOperation):
            continue
        label = _priority_label_for_title(request_text, operation.title)
        if label is not None:
            labeled_operations.append((operation_index, *label))
    if len(labeled_operations) < 2 or len(labeled_operations) != add_operation_count:
        return {}

    group_values = {group for _, group, _ in labeled_operations}
    number_values = {number for _, _, number in labeled_operations}
    group_relations, number_relations = _priority_relations(
        request_text,
        group_values,
        number_values,
    )

    group_order = _priority_token_order(group_values, group_relations)
    number_order = _priority_token_order(number_values, number_relations)
    if group_order is None or number_order is None:
        return {}
    group_position = {value: index for index, value in enumerate(group_order)}
    number_position = {value: index for index, value in enumerate(number_order)}

    def precedence_key(group: str, number: int) -> tuple[int, int]:
        return (
            group_position[group],
            number_position[number],
        )

    ordered_keys = sorted({
        precedence_key(group, number)
        for _, group, number in labeled_operations
    })
    score_by_key = {
        key: len(ordered_keys) - index
        for index, key in enumerate(ordered_keys)
    }
    return {
        operation_index: score_by_key[precedence_key(group, number)]
        for operation_index, group, number in labeled_operations
    }


def _priority_label_for_title(request_text: str, title: str) -> tuple[str, int] | None:
    title_label = _priority_label_in_model_title(request_text, title)
    if title_label is not None:
        return title_label[1], title_label[2]
    for line in request_text.splitlines():
        title_index = line.find(title)
        if title_index == -1:
            continue
        suffix = line[title_index + len(title) :]
        match = re.match(
            r"\s*[，,]\s*([A-Za-z]+)\s*[-_.]?\s*(\d+)\s*(?=[，,]|$)",
            suffix,
        )
        if match is not None:
            return match.group(1).upper(), int(match.group(2))
    return None


def _priority_label_in_model_title(
    request_text: str,
    title: str,
) -> tuple[str, str, int] | None:
    match = re.match(
        r"^(.+?)\s*[，,]\s*([A-Za-z]+)\s*[-_.]?\s*(\d+)\s*$",
        title,
    )
    if match is None or not any(title in line for line in request_text.splitlines()):
        return None
    return match.group(1).strip(), match.group(2).upper(), int(match.group(3))


def _priority_relations(
    request_text: str,
    group_values: set[str],
    number_values: set[int],
) -> tuple[set[tuple[str, str]], set[tuple[int, int]]]:
    normalized = (
        request_text.replace("大于", ">")
        .replace("高于", ">")
        .replace("优先于", ">")
        .replace("＞", ">")
    )
    group_relations: set[tuple[str, str]] = set()
    number_relations: set[tuple[int, int]] = set()
    for match in re.finditer(
        r"(?=\b([A-Za-z]+|\d+)\s*>\s*([A-Za-z]+|\d+)\b)",
        normalized,
    ):
        higher, lower = match.group(1), match.group(2)
        if higher.isalpha() and lower.isalpha():
            relation = (higher.upper(), lower.upper())
            if relation[0] in group_values and relation[1] in group_values:
                group_relations.add(relation)
        elif higher.isdigit() and lower.isdigit():
            relation = (int(higher), int(lower))
            if relation[0] in number_values and relation[1] in number_values:
                number_relations.add(relation)
    return group_relations, number_relations


def _priority_token_order(
    values: set[str] | set[int],
    relations: set[tuple[str, str]] | set[tuple[int, int]],
) -> list[str] | list[int] | None:
    outgoing = {value: set() for value in values}
    indegree = {value: 0 for value in values}
    for higher, lower in relations:
        if lower not in outgoing[higher]:
            outgoing[higher].add(lower)
            indegree[lower] += 1
    available = sorted(value for value, degree in indegree.items() if degree == 0)
    ordered = []
    while available:
        value = available.pop(0)
        ordered.append(value)
        for lower in sorted(outgoing[value]):
            indegree[lower] -= 1
            if indegree[lower] == 0:
                available.append(lower)
                available.sort()
    return ordered if len(ordered) == len(values) else None


def _next_temporary_id(
    *,
    request_id: str,
    operation_index: int,
    collision_index: int,
    uuid_factory: Callable[[], UUID] | None,
) -> UUID:
    if uuid_factory is not None:
        return uuid_factory()
    seed = f"{len(request_id)}:{request_id}:{operation_index}:{collision_index}"
    return uuid5(_TEMPORARY_ID_NAMESPACE, seed)


def _is_protected(item: TimeFragmentPlanItem) -> bool:
    if isinstance(item, TimeFragmentExternalEventItem):
        return True
    return item.is_pinned or item.is_completed


def _apply_clear_after_insertion_intent(
    request_text: str,
    base_items: list[TimeFragmentPlanItem],
    operations: list[TimeFragmentOperation],
) -> list[TimeFragmentOperation]:
    corrected: list[TimeFragmentOperation] = []
    for operation in operations:
        if not isinstance(operation, TimeFragmentAddOperation):
            corrected.append(operation)
            continue
        slot = _clear_after_insertion_slot(request_text, operation.title, base_items)
        corrected.append(
            operation.model_copy(
                update={"placement": TimeFragmentPlacement(anchor="start", slot=slot)},
                deep=True,
            )
            if slot is not None
            else operation
        )
    return corrected


def _clear_after_insertion_slot(
    request_text: str,
    added_title: str,
    base_items: list[TimeFragmentPlanItem],
) -> int | None:
    title_index = request_text.rfind(added_title)
    if title_index == -1:
        return None
    marker_matches = [
        (request_text.rfind(marker, 0, title_index), marker)
        for marker in ("之后", "以后", "后")
    ]
    marker_index, marker = max(
        marker_matches,
        key=lambda match: (match[0] + len(match[1]), len(match[1])),
    )
    if marker_index == -1:
        return None
    connector = request_text[marker_index + len(marker) : title_index]
    if _has_explicit_time_reference(connector) or connector.strip(" \t\r\n，,。；;！!？?、") not in {
        "",
        "就",
        "再",
        "然后",
        "立即",
        "马上",
        "直接",
    }:
        return None
    before = request_text[:marker_index]
    clause_start = max(
        (before.rfind(delimiter) for delimiter in "，,。；;！!？?\n"),
        default=-1,
    )
    before = before[clause_start + 1 :]
    normalized_before = before.replace("完成", "").replace("完", "")
    anchors = [
        item
        for item in base_items
        if item.segments
        and (item.title in before or item.title in normalized_before)
    ]
    if not anchors:
        return None
    anchor = max(anchors, key=lambda item: len(item.title))
    return max(segment.end_slot for segment in anchor.segments)


def _has_explicit_time_reference(text: str) -> bool:
    if any(
        marker in text
        for marker in ("上午", "中午", "下午", "晚上", "傍晚", "凌晨", "早上", "清晨", ":", "：")
    ):
        return True
    return _has_exact_clock_reference(text)


def _has_exact_clock_reference(text: str) -> bool:
    return _EXACT_CLOCK_PATTERN.search(text) is not None


def _add_placement_is_authorized(
    request_text: str,
    title: str,
    authorization_text: str | None,
) -> bool:
    if authorization_text is None:
        return False
    evidence = authorization_text.strip()
    if not evidence or evidence not in request_text or title not in evidence:
        return False
    if any(delimiter in evidence for delimiter in "。；;！!？?\n"):
        return False
    clause = _clause_containing(request_text, evidence)
    if any(marker in clause for marker in ("不要", "别", "无需", "不许", "不能", "禁止")):
        return False
    return _has_exact_clock_reference(evidence)


def _operation_is_explicitly_requested(
    request_text: str,
    item: TimeFragmentPlanItem,
    operation: TimeFragmentOperation,
    authorization_text: str | None,
) -> bool:
    if _protected_operation_is_authorized(
        request_text,
        item,
        operation,
        authorization_text,
    ):
        return True
    for identifier in (item.title, item.item_id):
        if identifier not in request_text:
            continue
        clause = _clause_containing(request_text, identifier)
        if _protected_operation_is_authorized(
            request_text,
            item,
            operation,
            clause,
        ):
            return True
    return False


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
    if operation.type == "delete":
        is_affirmative_buyaole = _is_affirmative_buyaole_delete_evidence(evidence, item)
        if "不要" in evidence and not is_affirmative_buyaole:
            return False
        if "不要" in clause and not _is_affirmative_buyaole_delete_evidence(clause, item):
            return False
        if is_affirmative_buyaole and _has_following_buyao_delete_negation(
            request_text,
            evidence,
        ):
            return False
    elif "不要" in clause:
        return False
    negative_markers = ("别", "无需", "不许", "不能", "禁止", "保持不变", "保持原样")
    operation_negative_markers = {
        "move": (),
        "changeDuration": (),
        "changeTitle": (),
        "delete": (
            "不删除",
            "不删掉",
            "不移除",
            "不取消",
        ),
    }[operation.type]
    if any(marker in clause for marker in negative_markers + operation_negative_markers):
        return False
    intent_terms = {
        "move": ("移动", "移到", "挪到", "改到", "调到", "重排", "安排", "调整"),
        "changeDuration": ("时长", "延长", "缩短", "改成", "调整时长", "修改时长"),
        "changeTitle": ("改标题", "修改标题", "标题改成", "重命名", "改名", "名称改成"),
        "delete": ("删除", "删掉", "移除", "取消", "不要了"),
    }[operation.type]
    if not any(term in evidence for term in intent_terms):
        return False
    if item.title in evidence or item.item_id in evidence:
        return True
    return _evidence_scope_contains_item(evidence, item)


def _is_affirmative_buyaole_delete_evidence(
    evidence: str,
    item: TimeFragmentPlanItem,
) -> bool:
    punctuation = " \t\r\n，,。；;！!？?、"
    normalized = evidence.strip(punctuation)
    for polite_prefix in ("请", "麻烦"):
        if normalized.startswith(polite_prefix):
            normalized = normalized[len(polite_prefix) :].lstrip(punctuation)
            break
    for polite_suffix in ("谢谢你", "谢谢", "麻烦了", "拜托了"):
        if normalized.endswith(polite_suffix):
            normalized = normalized[: -len(polite_suffix)].rstrip(punctuation)
            break
    return any(
        normalized == f"{target_reference}不要了"
        for target_reference in (item.title, item.item_id)
    )


def _has_following_buyao_delete_negation(request_text: str, evidence: str) -> bool:
    evidence_end = request_text.find(evidence) + len(evidence)
    following_text = request_text[evidence_end:]
    search_start = 0
    while (buyao_index := following_text.find("不要", search_start)) != -1:
        after_buyao = following_text[buyao_index + len("不要") :]
        if any(term in after_buyao for term in ("删除", "删掉", "移除", "取消")):
            return True
        search_start = buyao_index + len("不要")
    return False


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


def _first_available_slot(request: TimeFragmentPlanRequestV2) -> int:
    if request.earliest_start_slot is not None:
        return request.earliest_start_slot
    current_day_slot = _current_day_slot(request)
    return 0 if current_day_slot is None else current_day_slot


def _current_day_slot(request: TimeFragmentPlanRequestV2) -> int | None:
    parsed = datetime.fromisoformat(request.now.replace("Z", "+00:00"))
    if request.current_plan.date != parsed.date().isoformat():
        return None
    slot = parsed.hour * 4 + parsed.minute // 15
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        slot += 1
    return min(slot, 96)


def _placement_precedes_slot(
    placement: TimeFragmentPlacement,
    duration_slots: int,
    earliest_slot: int,
) -> bool:
    if placement.anchor == "start":
        return placement.slot < earliest_slot
    return placement.slot - duration_slots < earliest_slot


def _allocate_segments(
    occupied: list[bool],
    duration_slots: int,
    *,
    placement: TimeFragmentPlacement | None,
    earliest_slot: int,
    allow_occupied_anchor: bool = False,
) -> list[TimeFragmentSegmentV2]:
    if placement is None:
        if earliest_slot >= 96:
            return []
        candidates = range(earliest_slot, 96)
    elif placement.anchor == "start":
        if placement.slot < earliest_slot or placement.slot >= 96 or (
            occupied[placement.slot] and not allow_occupied_anchor
        ):
            return []
        candidates = range(placement.slot, 96)
    else:
        end = placement.slot
        if end <= 0 or occupied[end - 1]:
            return []
        candidates = range(end - 1, earliest_slot - 1, -1)

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
