import json
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

import pytest

from app.contracts import (
    TimeFragmentModelOperations,
    TimeFragmentPlanProposal,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanV2,
)
from app.time_fragment import plan_time_fragment, validate_time_fragment_proposal


FIXTURES = Path(__file__).parent / "fixtures" / "time-fragment-planner-v1"


def internal_item(
    item_id: str,
    title: str,
    duration_slots: int,
    segments: list[tuple[int, int]],
    *,
    pinned: bool = False,
    completed: bool = False,
) -> dict[str, Any]:
    return {
        "itemId": item_id,
        "objectType": "internalTask",
        "domainRef": {
            "taskId": f"task-{item_id}",
            "occurrenceId": item_id,
            "scheduledTaskId": f"scheduled-{item_id}" if segments else None,
        },
        "title": title,
        "durationSlots": duration_slots,
        "segments": [
            {"startSlot": start_slot, "endSlot": end_slot}
            for start_slot, end_slot in segments
        ],
        "isPinned": pinned,
        "isCompleted": completed,
    }


def external_item(
    item_id: str,
    title: str,
    duration_slots: int,
    segments: list[tuple[int, int]],
) -> dict[str, Any]:
    return {
        "itemId": item_id,
        "objectType": "externalEvent",
        "domainRef": {"externalEventId": item_id},
        "title": title,
        "durationSlots": duration_slots,
        "segments": [
            {"startSlot": start_slot, "endSlot": end_slot}
            for start_slot, end_slot in segments
        ],
        "isAllDay": False,
        "isFixed": True,
    }


def request_with_items(items: list[dict[str, Any]], *, now: str = "2026-08-24T08:00:00+08:00") -> TimeFragmentPlanRequestV2:
    return TimeFragmentPlanRequestV2.model_validate(
        {
            "text": "调整今天的计划",
            "requestID": "request-planner-test",
            "baseFingerprint": "sha256:planner-base",
            "currentPlan": {"date": "2026-08-24", "items": items},
            "now": now,
        }
    )


def model_output(operations: list[dict[str, Any]]) -> TimeFragmentModelOperations:
    return TimeFragmentModelOperations.model_validate({"operations": operations})


def item_by_id(response: Any, item_id: str) -> Any:
    assert response.proposal is not None
    return next(item for item in response.proposal.candidate_plan.items if item.item_id == item_id)


def issue_codes(response: Any) -> list[str]:
    return [issue.code for issue in response.validation.issues]


def test_golden_three_slot_task_splits_around_pinned_a() -> None:
    fixture = json.loads((FIXTURES / "split-around-pinned.json").read_text())
    request = TimeFragmentPlanRequestV2.model_validate(fixture["request"])
    operations = TimeFragmentModelOperations.model_validate(fixture["modelOutput"])

    response = plan_time_fragment(
        request,
        operations,
        uuid_factory=lambda: UUID(fixture["injectedTemporaryId"]),
    )

    assert response.model_dump(mode="json", by_alias=True) == fixture["expectedResponse"]
    task_b = item_by_id(response, fixture["injectedTemporaryId"])
    assert [(segment.start_slot, segment.end_slot) for segment in task_b.segments] == [
        (40, 41),
        (42, 44),
    ]


def test_empty_day_add_uses_server_uuid_default_30_minutes_and_earliest_remaining_slot() -> None:
    temporary_id = UUID("22222222-2222-4222-8222-222222222222")

    response = plan_time_fragment(
        request_with_items([]),
        model_output([{"type": "add", "title": "写方案", "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert response.proposal is not None
    assert response.proposal.algorithm_version == "time-fragment-planner-v1"
    operation = response.proposal.operations[0]
    assert str(operation.temporary_id) == str(temporary_id)
    assert operation.duration_slots == 2
    added = item_by_id(response, str(temporary_id))
    assert added.domain_ref is None
    assert [(segment.start_slot, segment.end_slot) for segment in added.segments] == [(32, 34)]


def test_unknown_target_is_not_guessed_from_matching_title_and_candidate_is_retained() -> None:
    request = request_with_items([internal_item("occurrence-1", "写方案", 2, [(36, 38)])])

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "missing-id",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 40},
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is False
    assert "UNKNOWN_TARGET" in issue_codes(response)
    retained = item_by_id(response, "occurrence-1")
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(36, 38)]


def test_object_type_mismatch_is_rejected_without_mutating_target() -> None:
    request = request_with_items([external_item("external-1", "客户会议", 2, [(40, 42)])])

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "external-1",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "inputOrder": 0,
                    "isExplicit": True,
                }
            ]
        ),
    )

    assert "UNKNOWN_TARGET" in issue_codes(response)
    retained = item_by_id(response, "external-1")
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(40, 42)]


@pytest.mark.parametrize("is_explicit", [False, True])
def test_pinned_task_requires_explicit_authorization_but_keeps_semantic_candidate(
    is_explicit: bool,
) -> None:
    request = request_with_items([internal_item("pinned-a", "A", 1, [(36, 37)], pinned=True)])

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 40},
                    "inputOrder": 0,
                    "isExplicit": is_explicit,
                }
            ]
        ),
        attempts=2,
    )

    moved = item_by_id(response, "pinned-a")
    assert [(segment.start_slot, segment.end_slot) for segment in moved.segments] == [(40, 41)]
    assert ("PROTECTED_OBJECT" in issue_codes(response)) is (not is_explicit)
    assert response.validation.valid is is_explicit
    assert response.validation.attempts == 2


def test_non_target_is_locked_while_target_moves_to_requested_start() -> None:
    request = request_with_items(
        [
            internal_item("target", "目标", 2, [(36, 38)]),
            internal_item("locked", "非目标", 2, [(40, 42)]),
        ]
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "target",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 38},
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "target").segments] == [
        (38, 40)
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "locked").segments] == [
        (40, 42)
    ]


def test_delete_operations_drive_typed_explicit_sets_and_exact_candidate_id_set() -> None:
    request = request_with_items(
        [
            internal_item("occurrence-delete", "删除内部", 2, [(36, 38)]),
            external_item("external-delete", "删除外部", 2, [(40, 42)]),
            internal_item("occurrence-keep", "保留", 2, [(44, 46)]),
        ]
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": "occurrence-delete",
                    "inputOrder": 0,
                },
                {
                    "type": "delete",
                    "targetItemId": "external-delete",
                    "inputOrder": 1,
                    "isExplicit": True,
                },
            ]
        ),
    )

    assert response.validation.valid is True
    assert response.proposal is not None
    assert response.proposal.deleted_occurrence_ids == ["occurrence-delete"]
    assert response.proposal.deleted_external_event_ids == ["external-delete"]
    assert [item.item_id for item in response.proposal.candidate_plan.items] == ["occurrence-keep"]


@pytest.mark.parametrize(
    ("first_operation", "second_operation", "scheduled_title"),
    [
        (
            {"type": "add", "title": "低优先", "durationSlots": 2, "priority": 1, "inputOrder": 0},
            {"type": "add", "title": "高优先", "durationSlots": 2, "priority": 2, "inputOrder": 1},
            "高优先",
        ),
        (
            {"type": "add", "title": "后输入", "durationSlots": 2, "priority": 1, "inputOrder": 1},
            {"type": "add", "title": "先输入", "durationSlots": 2, "priority": 1, "inputOrder": 0},
            "先输入",
        ),
    ],
)
def test_priority_then_input_order_controls_competing_placement(
    first_operation: dict[str, Any],
    second_operation: dict[str, Any],
    scheduled_title: str,
) -> None:
    ids: Iterator[UUID] = iter(
        [
            UUID("33333333-3333-4333-8333-333333333333"),
            UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    request = request_with_items([external_item("fixed", "固定占用", 62, [(34, 96)])])

    response = plan_time_fragment(
        request,
        model_output([first_operation, second_operation]),
        uuid_factory=lambda: next(ids),
    )

    new_items = [
        item for item in response.proposal.candidate_plan.items if item.domain_ref is None  # type: ignore[union-attr]
    ]
    scheduled = [item for item in new_items if item.segments]
    unscheduled = [item for item in new_items if not item.segments]
    assert [item.title for item in scheduled] == [scheduled_title]
    assert [(segment.start_slot, segment.end_slot) for segment in scheduled[0].segments] == [(32, 34)]
    assert len(unscheduled) == 1
    assert response.validation.valid is True
    assert "UNPLACED" in issue_codes(response)


def test_insufficient_capacity_returns_no_partial_segments_and_remains_valid() -> None:
    temporary_id = UUID("55555555-5555-4555-8555-555555555555")
    request = request_with_items([external_item("fixed", "固定占用", 63, [(33, 96)])])

    response = plan_time_fragment(
        request,
        model_output([{"type": "add", "title": "两格任务", "durationSlots": 2, "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert item_by_id(response, str(temporary_id)).segments == []
    assert response.validation.valid is True
    assert "UNPLACED" in issue_codes(response)
    assert "PARTIAL_PLACEMENT" not in issue_codes(response)


def test_end_anchor_is_honored_and_can_span_multiple_free_ranges() -> None:
    temporary_id = UUID("66666666-6666-4666-8666-666666666666")
    request = request_with_items([internal_item("locked", "锁定", 1, [(42, 43)], pinned=True)])

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "B",
                    "durationSlots": 3,
                    "placement": {"anchor": "end", "slot": 44},
                    "inputOrder": 0,
                }
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, str(temporary_id)).segments] == [
        (40, 42),
        (43, 44),
    ]


def test_candidate_validator_reports_id_partial_and_conflict_issues_without_throwing() -> None:
    request = request_with_items(
        [
            internal_item("one", "一", 2, [(36, 38)]),
            internal_item("two", "二", 2, [(40, 42)]),
        ]
    )
    valid_response = plan_time_fragment(request, model_output([]))
    assert valid_response.proposal is not None
    one = valid_response.proposal.candidate_plan.items[0].model_copy(
        update={"segments": valid_response.proposal.candidate_plan.items[0].segments[:1]},
        deep=True,
    )
    one = one.model_copy(update={"duration_slots": 3}, deep=True)
    two = valid_response.proposal.candidate_plan.items[1].model_copy(
        update={"segments": one.segments},
        deep=True,
    )
    invalid_plan = TimeFragmentPlanV2(
        date="2026-08-24",
        items=[one, two, two.model_copy(deep=True)],
    )
    invalid_proposal = TimeFragmentPlanProposal.model_validate(
        {
            **valid_response.proposal.model_dump(mode="json", by_alias=True),
            "candidatePlan": invalid_plan.model_dump(mode="json", by_alias=True),
        }
    )

    issues = validate_time_fragment_proposal(request, invalid_proposal)
    codes = {issue.code for issue in issues}

    assert {"ID_SET_MISMATCH", "PARTIAL_PLACEMENT", "CONFLICT"} <= codes


def test_server_uuid_injection_skips_a_collision_with_existing_item_id() -> None:
    collision = UUID("77777777-7777-4777-8777-777777777777")
    unique = UUID("88888888-8888-4888-8888-888888888888")
    generated: Iterator[UUID] = iter([collision, unique])
    request = request_with_items([internal_item(str(collision), "已有", 2, [(36, 38)])])

    response = plan_time_fragment(
        request,
        model_output([{"type": "add", "title": "新增", "inputOrder": 0}]),
        uuid_factory=lambda: next(generated),
    )

    assert response.proposal is not None
    add_operation = response.proposal.operations[0]
    assert str(add_operation.temporary_id) == str(unique)
    assert {item.item_id for item in response.proposal.candidate_plan.items} == {
        str(collision),
        str(unique),
    }
