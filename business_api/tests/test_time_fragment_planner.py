import hashlib
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
MANIFEST = FIXTURES / "manifest.json"
EXPECTED_GOLDEN_CASES = {
    "add-uuid-collision-retry",
    "change-title-existing-internal",
    "completed-unplaced-remains-completed",
    "cross-day-boundary-todo",
    "delete-existing-internal",
    "delete-temporary",
    "empty-day-default-duration",
    "external-multi-gap-segments",
    "past-explicit-move",
    "past-history-unchanged",
    "priority-input-order",
    "protected-external-authorized",
    "protected-external-change-duration-authorized",
    "protected-external-delete-authorized",
    "protected-external-unauthorized",
    "protected-pinned-authorized",
    "protected-pinned-change-duration-authorized",
    "protected-pinned-unauthorized",
    "split-around-pinned",
    "temporary-item-stable-id",
    "two-add-uuid-collision-retry",
    "unknown-target-failed-candidate",
    "unplaced-external-todo",
    "unplaced-internal-todo",
}


def golden_fixture_paths() -> tuple[Path, ...]:
    manifest = json.loads(MANIFEST.read_bytes())
    return tuple(FIXTURES / entry["file"] for entry in manifest["fixtures"])


GOLDEN_FIXTURE_PATHS = golden_fixture_paths()


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


def request_with_items(
    items: list[dict[str, Any]],
    *,
    date: str = "2026-08-24",
    earliest_start_slot: int | None = None,
    now: str = "2026-08-24T08:00:00+08:00",
    text: str = "调整今天的计划",
) -> TimeFragmentPlanRequestV2:
    payload = {
        "text": text,
        "requestID": "request-planner-test",
        "baseFingerprint": "sha256:planner-base",
        "currentPlan": {"date": date, "items": items},
        "now": now,
    }
    if earliest_start_slot is not None:
        payload["earliestStartSlot"] = earliest_start_slot
    return TimeFragmentPlanRequestV2.model_validate(payload)


def model_output(operations: list[dict[str, Any]]) -> TimeFragmentModelOperations:
    return TimeFragmentModelOperations.model_validate({"operations": operations})


def item_by_id(response: Any, item_id: str) -> Any:
    assert response.proposal is not None
    return next(item for item in response.proposal.candidate_plan.items if item.item_id == item_id)


def issue_codes(response: Any) -> list[str]:
    return [issue.code for issue in response.validation.issues]


def run_golden_fixture(fixture_path: Path) -> tuple[dict[str, Any], Any]:
    fixture = json.loads(fixture_path.read_bytes())
    request = TimeFragmentPlanRequestV2.model_validate(fixture["request"])
    operations = TimeFragmentModelOperations.model_validate(fixture["modelOutput"])
    injected_uuids: Iterator[UUID] = iter(
        UUID(value) for value in fixture["injectedUUIDs"]
    )
    consumed_uuids: list[str] = []

    def uuid_factory() -> UUID:
        value = next(injected_uuids)
        consumed_uuids.append(str(value))
        return value

    response = plan_time_fragment(
        request,
        operations,
        uuid_factory=uuid_factory,
    )
    assert consumed_uuids == fixture["injectedUUIDs"]
    return fixture, response


def contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(contains_key(child, key) for child in value)
    return False


def golden_response(case_name: str) -> dict[str, Any]:
    return golden_fixture(case_name)["expectedResponse"]


def golden_fixture(case_name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{case_name}.json").read_bytes())


def golden_segments(response: dict[str, Any], item_id: str) -> list[tuple[int, int]]:
    item = next(
        item
        for item in response["proposal"]["candidatePlan"]["items"]
        if item["itemId"] == item_id
    )
    return [(segment["startSlot"], segment["endSlot"]) for segment in item["segments"]]


def golden_issue_codes(response: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in response["validation"]["issues"]]


@pytest.mark.parametrize(
    "fixture_path",
    GOLDEN_FIXTURE_PATHS,
    ids=lambda fixture_path: fixture_path.stem,
)
def test_time_fragment_planner_golden_fixture(fixture_path: Path) -> None:
    fixture, response = run_golden_fixture(fixture_path)
    actual = response.model_dump(mode="json", by_alias=True)

    assert set(fixture) == {
        "case",
        "request",
        "modelOutput",
        "injectedUUIDs",
        "expectedResponse",
    }
    assert actual == fixture["expectedResponse"]
    assert actual["requestID"] == fixture["request"]["requestID"]
    assert actual["proposal"]["baseFingerprint"] == fixture["request"]["baseFingerprint"]
    assert actual["proposal"]["candidatePlan"]["date"] == fixture["request"][
        "currentPlan"
    ]["date"]
    assert actual["validation"]["attempts"] == 1
    for private_key in ("authorizationText", "isExplicit", "status"):
        assert contains_key(actual, private_key) is False
    for item in actual["proposal"]["candidatePlan"]["items"]:
        for segment in item["segments"]:
            assert 0 <= segment["startSlot"] < segment["endSlot"] <= 96


def test_golden_fixture_manifest_matches_exact_bytes_and_case_set() -> None:
    manifest = json.loads(MANIFEST.read_bytes())
    entries = manifest["fixtures"]
    listed_names = [entry["file"] for entry in entries]
    actual_names = {
        path.name for path in FIXTURES.glob("*.json") if path.name != MANIFEST.name
    }

    assert manifest["hashAlgorithm"] == "SHA-256"
    assert "excluded" in manifest["scope"]
    assert listed_names == sorted(listed_names)
    assert len(listed_names) == len(set(listed_names))
    assert set(listed_names) == actual_names
    assert {Path(name).stem for name in listed_names} == EXPECTED_GOLDEN_CASES
    for entry in entries:
        fixture_bytes = (FIXTURES / entry["file"]).read_bytes()
        assert hashlib.sha256(fixture_bytes).hexdigest() == entry["sha256"]


def test_golden_fixtures_cover_required_planner_semantics() -> None:
    empty = golden_response("empty-day-default-duration")
    empty_item = empty["proposal"]["candidatePlan"]["items"][0]
    assert empty_item["durationSlots"] == 2
    assert golden_segments(empty, empty_item["itemId"]) == [(32, 34)]

    past = golden_response("past-history-unchanged")
    moved_past = golden_response("past-explicit-move")
    assert golden_segments(past, "past-history") == [(20, 22)]
    assert golden_segments(moved_past, "past-move") == [(48, 50)]

    for case_name in (
        "protected-pinned-unauthorized",
        "protected-external-unauthorized",
    ):
        fixture = golden_fixture(case_name)
        response = fixture["expectedResponse"]
        baseline_item = fixture["request"]["currentPlan"]["items"][0]
        requested_slot = fixture["modelOutput"]["operations"][0]["placement"]["slot"]
        assert requested_slot != baseline_item["segments"][0]["startSlot"]
        assert golden_issue_codes(response) == ["PROTECTED_OBJECT"]
        assert response["proposal"]["operations"] == []
        assert response["proposal"]["candidatePlan"]["items"] == [baseline_item]

    pinned_authorized = golden_response("protected-pinned-authorized")
    external_authorized = golden_response("protected-external-authorized")
    assert pinned_authorized["validation"]["valid"] is True
    assert golden_segments(pinned_authorized, "pinned-authorized") == [(40, 41)]
    assert external_authorized["validation"]["valid"] is True
    assert golden_segments(external_authorized, "external-authorized") == [(60, 62)]

    external_delete_fixture = golden_fixture("protected-external-delete-authorized")
    external_delete = external_delete_fixture["expectedResponse"]
    assert external_delete["validation"]["valid"] is True
    assert [operation["type"] for operation in external_delete["proposal"]["operations"]] == [
        "delete"
    ]
    assert external_delete["proposal"]["deletedExternalEventIDs"] == [
        "external-delete-authorized"
    ]
    base_ids = {
        item["itemId"]
        for item in external_delete_fixture["request"]["currentPlan"]["items"]
    }
    deleted_ids = {
        operation["targetItemId"]
        for operation in external_delete["proposal"]["operations"]
        if operation["type"] == "delete"
    }
    added_ids = {
        operation["temporaryId"]
        for operation in external_delete["proposal"]["operations"]
        if operation["type"] == "add"
    }
    candidate_ids = {
        item["itemId"] for item in external_delete["proposal"]["candidatePlan"]["items"]
    }
    assert candidate_ids == (base_ids - deleted_ids) | added_ids

    collision_fixture = golden_fixture("add-uuid-collision-retry")
    collision = collision_fixture["expectedResponse"]
    collision_id, unique_id = collision_fixture["injectedUUIDs"]
    assert collision_id in {
        item["itemId"] for item in collision_fixture["request"]["currentPlan"]["items"]
    }
    assert collision["proposal"]["operations"][0]["temporaryId"] == unique_id
    assert {
        item["itemId"] for item in collision["proposal"]["candidatePlan"]["items"]
    } == {collision_id, unique_id}

    same_batch_collision_fixture = golden_fixture("two-add-uuid-collision-retry")
    same_batch_collision = same_batch_collision_fixture["expectedResponse"]
    first_id, repeated_id, second_id = same_batch_collision_fixture["injectedUUIDs"]
    assert repeated_id == first_id
    assert [
        operation["temporaryId"]
        for operation in same_batch_collision["proposal"]["operations"]
    ] == [first_id, second_id]
    assert golden_segments(same_batch_collision, first_id) == [(32, 34)]
    assert golden_segments(same_batch_collision, second_id) == [(34, 36)]

    title_changed = golden_response("change-title-existing-internal")
    title_item = title_changed["proposal"]["candidatePlan"]["items"][0]
    assert title_changed["proposal"]["operations"][0]["allowedChanges"] == ["title"]
    assert title_item["title"] == "新标题"
    assert golden_segments(title_changed, "occurrence-title") == [(36, 37), (40, 42)]

    completed_unplaced = golden_response("completed-unplaced-remains-completed")
    completed_item = next(
        item
        for item in completed_unplaced["proposal"]["candidatePlan"]["items"]
        if item["itemId"] == "completed-unplaced"
    )
    assert completed_unplaced["validation"]["valid"] is True
    assert golden_issue_codes(completed_unplaced) == ["UNPLACED"]
    assert completed_item["segments"] == []
    assert completed_item["isCompleted"] is True

    external_split = golden_response("external-multi-gap-segments")
    assert external_split["validation"]["valid"] is True
    assert golden_segments(external_split, "external-multi-gap") == [(40, 41), (42, 44)]

    temporary_fixture = golden_fixture("temporary-item-stable-id")
    temporary = temporary_fixture["expectedResponse"]
    temporary_id = temporary_fixture["request"]["currentPlan"]["items"][0]["itemId"]
    assert temporary_fixture["request"]["currentPlan"]["items"][0]["domainRef"] is None
    assert temporary_fixture["injectedUUIDs"] == []
    assert {
        operation["targetItemId"] for operation in temporary["proposal"]["operations"]
    } == {temporary_id}
    assert [item["itemId"] for item in temporary["proposal"]["candidatePlan"]["items"]] == [
        temporary_id
    ]
    assert temporary["proposal"]["candidatePlan"]["items"][0]["domainRef"] is None
    assert temporary["proposal"]["candidatePlan"]["items"][0]["durationSlots"] == 3
    assert golden_segments(temporary, temporary_id) == [(44, 47)]

    for case_name, item_id, expected_segments in (
        (
            "protected-pinned-change-duration-authorized",
            "pinned-duration-authorized",
            [(36, 40)],
        ),
        (
            "protected-external-change-duration-authorized",
            "external-duration-authorized",
            [(40, 44)],
        ),
    ):
        duration_response = golden_response(case_name)
        assert duration_response["validation"]["valid"] is True
        assert duration_response["proposal"]["operations"][0]["type"] == "changeDuration"
        assert duration_response["proposal"]["operations"][0]["durationSlots"] == 4
        assert golden_segments(duration_response, item_id) == expected_segments

    deleted_internal = golden_response("delete-existing-internal")
    deleted_temporary = golden_response("delete-temporary")
    assert deleted_internal["proposal"]["deletedOccurrenceIDs"] == [
        "occurrence-delete"
    ]
    assert deleted_internal["proposal"]["deletedExternalEventIDs"] == []
    assert deleted_temporary["proposal"]["candidatePlan"]["items"] == []
    assert deleted_temporary["proposal"]["deletedOccurrenceIDs"] == []
    assert deleted_temporary["proposal"]["deletedExternalEventIDs"] == []

    for case_name, item_id in (
        ("unplaced-internal-todo", "10000000-0000-4000-8000-000000000002"),
        ("unplaced-external-todo", "external-unplaced"),
    ):
        unplaced = golden_response(case_name)
        assert unplaced["validation"]["valid"] is True
        assert golden_issue_codes(unplaced) == ["UNPLACED"]
        assert golden_segments(unplaced, item_id) == []

    priority = golden_response("priority-input-order")
    assert golden_segments(priority, "10000000-0000-4000-8000-000000000003") == []
    assert golden_segments(priority, "10000000-0000-4000-8000-000000000004") == []
    assert golden_segments(priority, "10000000-0000-4000-8000-000000000005") == [
        (32, 34)
    ]

    unknown = golden_response("unknown-target-failed-candidate")
    assert unknown["proposal"] is not None
    assert unknown["validation"]["valid"] is False
    assert golden_issue_codes(unknown) == ["UNKNOWN_TARGET"]
    assert golden_segments(unknown, "known-item") == [(36, 38)]

    boundary = golden_response("cross-day-boundary-todo")
    assert boundary["validation"]["valid"] is False
    assert golden_issue_codes(boundary) == ["INVALID_TIME", "UNPLACED"]
    assert golden_segments(
        boundary, "10000000-0000-4000-8000-000000000006"
    ) == []


def test_golden_three_slot_task_splits_around_pinned_a() -> None:
    fixture, response = run_golden_fixture(FIXTURES / "split-around-pinned.json")

    task_b = item_by_id(response, fixture["injectedUUIDs"][0])
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


def test_today_add_starts_at_app_supplied_earliest_slot() -> None:
    temporary_id = UUID("26262626-2626-4262-8262-262626262626")
    response = plan_time_fragment(
        request_with_items(
            [],
            earliest_start_slot=48,
            now="2026-08-24T10:15:59+08:00",
        ),
        model_output([{"type": "add", "title": "当天任务", "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(
        response,
        str(temporary_id),
    ).segments] == [(48, 50)]


def test_future_candidate_matching_selected_date_is_not_cross_day() -> None:
    response = plan_time_fragment(
        request_with_items([], date="2026-08-25"),
        model_output([]),
    )

    assert response.validation.valid is True
    assert "CROSS_DAY" not in issue_codes(response)
    assert response.proposal is not None
    assert response.proposal.candidate_plan.date == "2026-08-25"


def test_future_add_starts_at_app_supplied_earliest_slot() -> None:
    temporary_id = UUID("23232323-2323-4232-8232-232323232323")
    response = plan_time_fragment(
        request_with_items(
            [],
            date="2026-08-25",
            earliest_start_slot=36,
        ),
        model_output([{"type": "add", "title": "未来任务", "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(
        response,
        str(temporary_id),
    ).segments] == [(36, 38)]


def test_future_earliest_slot_preserves_unchanged_existing_earlier_task() -> None:
    temporary_id = UUID("25252525-2525-4252-8252-252525252525")
    response = plan_time_fragment(
        request_with_items(
            [internal_item("early-existing", "原有早间任务", 2, [(28, 30)])],
            date="2026-08-25",
            earliest_start_slot=36,
        ),
        model_output([{"type": "add", "title": "未来任务", "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(
        response,
        "early-existing",
    ).segments] == [(28, 30)]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(
        response,
        str(temporary_id),
    ).segments] == [(36, 38)]


def test_future_explicit_add_before_app_earliest_slot_is_rejected() -> None:
    temporary_id = UUID("24242424-2424-4242-8242-242424242424")
    response = plan_time_fragment(
        request_with_items(
            [],
            date="2026-08-25",
            earliest_start_slot=36,
        ),
        model_output([
            {
                "type": "add",
                "title": "过早任务",
                "placement": {"anchor": "start", "slot": 32},
                "inputOrder": 0,
            }
        ]),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is False
    assert item_by_id(response, str(temporary_id)).segments == []
    assert "INVALID_TIME" in issue_codes(response)


def test_default_temporary_ids_make_identical_request_and_operations_byte_identical() -> None:
    request = request_with_items([])
    operations = model_output(
        [
            {"type": "add", "title": "第一项", "inputOrder": 0},
            {"type": "add", "title": "第二项", "inputOrder": 1},
        ]
    )

    first = plan_time_fragment(request, operations)
    second = plan_time_fragment(request, operations)

    assert first.proposal is not None
    assert second.proposal is not None
    assert first.proposal.model_dump_json(by_alias=True) == second.proposal.model_dump_json(
        by_alias=True
    )
    temporary_ids = [operation.temporary_id for operation in first.proposal.operations]
    assert len(set(temporary_ids)) == 2
    assert all(temporary_id.version == 5 for temporary_id in temporary_ids)


def test_default_temporary_id_collision_retry_is_stable() -> None:
    operations = model_output([{"type": "add", "title": "新增", "inputOrder": 0}])
    first = plan_time_fragment(request_with_items([]), operations)
    assert first.proposal is not None
    colliding_id = str(first.proposal.operations[0].temporary_id)
    collision_request = request_with_items(
        [internal_item(colliding_id, "已有", 2, [(36, 38)])]
    )

    collided = plan_time_fragment(collision_request, operations)
    repeated = plan_time_fragment(collision_request, operations)

    assert collided.proposal is not None
    assert repeated.proposal is not None
    replacement_id = str(collided.proposal.operations[0].temporary_id)
    assert replacement_id != colliding_id
    assert UUID(replacement_id).version == 5
    assert collided.proposal.model_dump_json(by_alias=True) == repeated.proposal.model_dump_json(
        by_alias=True
    )


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


def test_duplicate_moves_for_unknown_target_report_only_unknown_target() -> None:
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
                },
                {
                    "type": "move",
                    "targetItemId": "missing-id",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "inputOrder": 1,
                },
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is False
    assert issue_codes(response) == ["UNKNOWN_TARGET"]
    assert response.validation.issues[0].item_id == "missing-id"
    assert response.validation.issues[0].field == "targetItemId"
    assert [operation.type for operation in response.proposal.operations] == ["move", "move"]
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
                }
            ]
        ),
    )

    assert issue_codes(response) == ["UNKNOWN_TARGET"]
    assert response.proposal is not None
    assert [operation.type for operation in response.proposal.operations] == ["move"]
    retained = item_by_id(response, "external-1")
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(40, 42)]


def test_duplicate_protected_operations_report_only_invalid_operation_and_keep_evidence() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 2, [(36, 38)], pinned=True)],
        text="整理今天的计划",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "inputOrder": 0,
                },
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 48},
                    "inputOrder": 1,
                },
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["INVALID_OPERATION"]
    assert [operation.type for operation in response.proposal.operations] == ["move", "move"]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned-a").segments] == [
        (36, 38)
    ]


def test_conflicting_priorities_on_protected_target_are_classified_before_authorization() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 2, [(36, 38)], pinned=True)],
        text="整理今天的计划",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "objectType": "internalTask",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "priority": 2,
                    "inputOrder": 0,
                },
                {
                    "type": "changeDuration",
                    "targetItemId": "pinned-a",
                    "objectType": "internalTask",
                    "allowedChanges": ["durationSlots", "segments"],
                    "durationSlots": 4,
                    "priority": 1,
                    "inputOrder": 1,
                },
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["INVALID_OPERATION"]
    assert [operation.type for operation in response.proposal.operations] == [
        "move",
        "changeDuration",
    ]
    retained = item_by_id(response, "pinned-a")
    assert retained.duration_slots == 2
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(36, 38)]


def test_change_title_updates_only_existing_internal_title_without_rescheduling() -> None:
    request = request_with_items(
        [internal_item("occurrence-title", "旧标题", 3, [(36, 37), (40, 42)])],
        text="把旧标题改标题为新标题",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "changeTitle",
                    "targetItemId": "occurrence-title",
                    "objectType": "internalTask",
                    "title": "新标题",
                    "allowedChanges": ["title"],
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert response.proposal is not None
    assert response.proposal.deleted_occurrence_ids == []
    assert response.proposal.deleted_external_event_ids == []
    assert response.proposal.operations[0].model_dump(mode="json", by_alias=True) == {
        "type": "changeTitle",
        "targetItemId": "occurrence-title",
        "objectType": "internalTask",
        "title": "新标题",
        "allowedChanges": ["title"],
        "inputOrder": 0,
    }
    changed = item_by_id(response, "occurrence-title")
    assert changed.title == "新标题"
    assert changed.duration_slots == 3
    assert [(segment.start_slot, segment.end_slot) for segment in changed.segments] == [
        (36, 37),
        (40, 42),
    ]


@pytest.mark.parametrize(
    ("pinned", "completed"),
    [(True, False), (False, True)],
)
def test_change_title_requires_and_accepts_exact_authorization_for_protected_internal_task(
    pinned: bool,
    completed: bool,
) -> None:
    item_id = "protected-title"
    baseline = internal_item(
        item_id,
        "旧标题",
        2,
        [(36, 38)],
        pinned=pinned,
        completed=completed,
    )
    operation = {
        "type": "changeTitle",
        "targetItemId": item_id,
        "objectType": "internalTask",
        "title": "新标题",
        "allowedChanges": ["title"],
        "inputOrder": 0,
    }

    unauthorized = plan_time_fragment(
        request_with_items([baseline], text="保持旧标题不变"),
        model_output([operation]),
    )
    authorized_text = "把旧标题改标题为新标题"
    authorized = plan_time_fragment(
        request_with_items([baseline], text=authorized_text),
        model_output([{**operation, "authorizationText": authorized_text}]),
    )

    assert unauthorized.proposal is not None
    assert issue_codes(unauthorized) == ["PROTECTED_OBJECT"]
    assert unauthorized.proposal.operations == []
    assert item_by_id(unauthorized, item_id).title == "旧标题"
    assert authorized.validation.valid is True
    assert authorized.proposal is not None
    assert item_by_id(authorized, item_id).title == "新标题"
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(authorized, item_id).segments] == [
        (36, 38)
    ]
    serialized = authorized.proposal.model_dump(mode="json", by_alias=True)
    assert contains_key(serialized, "authorizationText") is False
    assert contains_key(serialized, "isExplicit") is False
    assert contains_key(serialized, "status") is False


def test_external_event_title_is_source_fact_and_cannot_be_changed() -> None:
    request = request_with_items(
        [external_item("external-title", "来源标题", 2, [(40, 42)])],
        text="把来源标题改标题为新标题",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "changeTitle",
                    "targetItemId": "external-title",
                    "objectType": "internalTask",
                    "title": "新标题",
                    "allowedChanges": ["title"],
                    "authorizationText": "把来源标题改标题为新标题",
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert issue_codes(response) == ["UNKNOWN_TARGET"]
    retained = item_by_id(response, "external-title")
    assert retained.title == "来源标题"
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(40, 42)]


def test_two_default_adds_retry_same_batch_uuid_collision_and_scan_next_gap() -> None:
    first_id = UUID("30000000-0000-4000-8000-000000000001")
    second_id = UUID("30000000-0000-4000-8000-000000000002")
    injected = iter([first_id, first_id, second_id])
    consumed: list[UUID] = []

    def uuid_factory() -> UUID:
        value = next(injected)
        consumed.append(value)
        return value

    response = plan_time_fragment(
        request_with_items([], text="新增 A 和 B"),
        model_output(
            [
                {"type": "add", "title": "A", "inputOrder": 0},
                {"type": "add", "title": "B", "inputOrder": 1},
            ]
        ),
        uuid_factory=uuid_factory,
    )

    assert consumed == [first_id, first_id, second_id]
    assert response.validation.valid is True
    assert response.proposal is not None
    assert [str(operation.temporary_id) for operation in response.proposal.operations] == [
        str(first_id),
        str(second_id),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, str(first_id)).segments] == [
        (32, 34)
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, str(second_id)).segments] == [
        (34, 36)
    ]


@pytest.mark.parametrize("operation_type", ["move", "delete"])
def test_unauthorized_completed_move_or_delete_preserves_entire_baseline(
    operation_type: str,
) -> None:
    baseline = internal_item(
        "completed-protected",
        "已完成任务",
        2,
        [(36, 38)],
        completed=True,
    )
    operation: dict[str, Any] = {
        "type": operation_type,
        "targetItemId": "completed-protected",
        "objectType": "internalTask",
        "inputOrder": 0,
    }
    if operation_type == "move":
        operation.update(
            {
                "allowedChanges": ["segments"],
                "placement": {"anchor": "start", "slot": 44},
            }
        )

    response = plan_time_fragment(
        request_with_items([baseline], text="保持已完成任务不变"),
        model_output([operation]),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    assert response.proposal.deleted_occurrence_ids == []
    assert response.proposal.deleted_external_event_ids == []
    retained = item_by_id(response, "completed-protected")
    assert retained.title == "已完成任务"
    assert retained.is_completed is True
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(36, 38)]


def test_unauthorized_pinned_move_to_different_slot_retains_baseline_segments() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 2, [(36, 38)], pinned=True)],
        text="保持 A 不变",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is False
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned-a").segments] == [
        (36, 38)
    ]
    assert response.proposal.operations == []


def test_unauthorized_external_event_move_to_different_slot_retains_baseline_segments() -> None:
    request = request_with_items(
        [external_item("external-protected", "客户会议", 2, [(40, 42)])],
        text="保持客户会议不变",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "external-protected",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 48},
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is False
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, "external-protected").segments
    ] == [(40, 42)]
    assert response.proposal.operations == []


def test_any_unauthorized_operation_rejects_all_operations_for_protected_target() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 2, [(36, 38)], pinned=True)],
        text="移动 A 到 11:00",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 44},
                    "authorizationText": "移动 A 到 11:00",
                    "inputOrder": 0,
                },
                {
                    "type": "changeDuration",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["durationSlots", "segments"],
                    "durationSlots": 4,
                    "inputOrder": 1,
                },
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    retained = item_by_id(response, "pinned-a")
    assert retained.duration_slots == 2
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(36, 38)]


def test_unauthorized_external_delete_has_no_public_or_candidate_deletion() -> None:
    request = request_with_items(
        [external_item("external-protected", "客户会议", 2, [(40, 42)])],
        text="保持客户会议不变",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": "external-protected",
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    assert response.proposal.deleted_external_event_ids == []
    assert response.proposal.deleted_occurrence_ids == []
    assert [item.item_id for item in response.proposal.candidate_plan.items] == [
        "external-protected"
    ]


@pytest.mark.parametrize(
    ("item_id", "title", "object_type", "pinned", "completed"),
    [
        ("external-delete", "客户会议", "externalEvent", False, False),
        ("pinned-delete", "钉住任务", "internalTask", True, False),
        ("completed-delete", "已完成任务", "internalTask", False, True),
    ],
)
def test_buyaole_is_affirmative_delete_authorization_for_protected_target(
    item_id: str,
    title: str,
    object_type: str,
    pinned: bool,
    completed: bool,
) -> None:
    item = (
        external_item(item_id, title, 2, [(40, 42)])
        if object_type == "externalEvent"
        else internal_item(
            item_id,
            title,
            2,
            [(40, 42)],
            pinned=pinned,
            completed=completed,
        )
    )
    authorization_text = f"{title}不要了"

    response = plan_time_fragment(
        request_with_items([item], text=authorization_text),
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": item_id,
                    "objectType": object_type,
                    "authorizationText": authorization_text,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is True
    assert [operation.type for operation in response.proposal.operations] == ["delete"]
    assert response.proposal.candidate_plan.items == []
    assert response.proposal.deleted_external_event_ids == (
        [item_id] if object_type == "externalEvent" else []
    )
    assert response.proposal.deleted_occurrence_ids == (
        [item_id] if object_type == "internalTask" else []
    )


@pytest.mark.parametrize(
    "authorization_text",
    [
        "不要删除客户会议",
        "客户会议不要删掉",
        "别删除客户会议",
        "保持客户会议不变",
    ],
)
def test_negative_delete_language_does_not_authorize_protected_target(
    authorization_text: str,
) -> None:
    response = plan_time_fragment(
        request_with_items(
            [external_item("external-protected", "客户会议", 2, [(40, 42)])],
            text=authorization_text,
        ),
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": "external-protected",
                    "objectType": "externalEvent",
                    "authorizationText": authorization_text,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    assert response.proposal.deleted_external_event_ids == []
    assert response.proposal.deleted_occurrence_ids == []
    assert [item.item_id for item in response.proposal.candidate_plan.items] == [
        "external-protected"
    ]


@pytest.mark.parametrize(
    ("authorization_text", "item_id", "title", "object_type", "pinned", "completed"),
    [
        (
            "不要把客户会议删除",
            "external-protected",
            "客户会议",
            "externalEvent",
            False,
            False,
        ),
        (
            "请不要将客户会议删除",
            "external-protected",
            "客户会议",
            "externalEvent",
            False,
            False,
        ),
        (
            "不要再删除钉住任务",
            "pinned-protected",
            "钉住任务",
            "internalTask",
            True,
            False,
        ),
        (
            "不要给我删除已完成任务",
            "completed-protected",
            "已完成任务",
            "internalTask",
            False,
            True,
        ),
    ],
)
def test_delete_negation_with_intervening_words_does_not_authorize_protected_target(
    authorization_text: str,
    item_id: str,
    title: str,
    object_type: str,
    pinned: bool,
    completed: bool,
) -> None:
    item = (
        external_item(item_id, title, 2, [(40, 42)])
        if object_type == "externalEvent"
        else internal_item(
            item_id,
            title,
            2,
            [(40, 42)],
            pinned=pinned,
            completed=completed,
        )
    )

    response = plan_time_fragment(
        request_with_items([item], text=authorization_text),
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": item_id,
                    "objectType": object_type,
                    "authorizationText": authorization_text,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    assert response.proposal.deleted_external_event_ids == []
    assert response.proposal.deleted_occurrence_ids == []
    assert [item.item_id for item in response.proposal.candidate_plan.items] == [item_id]


def test_buyaole_followed_by_delete_negation_does_not_authorize_protected_target() -> None:
    authorization_text = "客户会议不要了，请不要再删除客户会议"

    response = plan_time_fragment(
        request_with_items(
            [external_item("external-protected", "客户会议", 2, [(40, 42)])],
            text=authorization_text,
        ),
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": "external-protected",
                    "objectType": "externalEvent",
                    "authorizationText": authorization_text,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert issue_codes(response) == ["PROTECTED_OBJECT"]
    assert response.proposal.operations == []
    assert response.proposal.deleted_external_event_ids == []
    assert [item.item_id for item in response.proposal.candidate_plan.items] == [
        "external-protected"
    ]


@pytest.mark.parametrize(
    "authorization_text",
    [
        "客户会议不要了。",
        "客户会议不要了，谢谢。",
    ],
)
def test_buyaole_allows_trailing_politeness_and_punctuation(
    authorization_text: str,
) -> None:
    response = plan_time_fragment(
        request_with_items(
            [external_item("external-protected", "客户会议", 2, [(40, 42)])],
            text=authorization_text,
        ),
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": "external-protected",
                    "objectType": "externalEvent",
                    "authorizationText": authorization_text,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is True
    assert [operation.type for operation in response.proposal.operations] == ["delete"]
    assert response.proposal.deleted_external_event_ids == ["external-protected"]
    assert response.proposal.candidate_plan.items == []


def test_named_pinned_task_accepts_verbatim_affirmative_authorization() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 1, [(36, 37)], pinned=True)],
        text="移动 A 到 10:00",
    )

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
                    "authorizationText": "移动 A 到 10:00",
                }
            ]
        ),
    )

    moved = item_by_id(response, "pinned-a")
    assert [(segment.start_slot, segment.end_slot) for segment in moved.segments] == [(40, 41)]
    assert response.validation.valid is True
    assert response.proposal is not None
    assert "authorizationText" not in response.proposal.model_dump(mode="json", by_alias=True)[
        "operations"
    ][0]


def test_afternoon_scope_authorizes_pinned_target_inside_the_scope() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 1, [(56, 57)], pinned=True)],
        text="重排整个下午",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 60},
                    "inputOrder": 0,
                    "authorizationText": "重排整个下午",
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned-a").segments] == [
        (60, 61)
    ]


@pytest.mark.parametrize(
    ("text", "authorization_text"),
    [
        ("把客户会议移到 15:00", "把客户会议移到 15:00"),
        ("重排整个下午", "重排整个下午"),
    ],
)
def test_exact_name_or_afternoon_scope_authorizes_external_event_move(
    text: str,
    authorization_text: str,
) -> None:
    request = request_with_items(
        [external_item("external-1", "客户会议", 2, [(56, 58)])],
        text=text,
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "external-1",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 60},
                    "inputOrder": 0,
                    "authorizationText": authorization_text,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "external-1").segments] == [
        (60, 62)
    ]


def test_negative_request_cannot_be_forged_into_protected_authorization() -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 1, [(36, 37)], pinned=True)],
        text="保留 A，不要移动",
    )

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
                    "authorizationText": "移动",
                }
            ]
        ),
    )

    assert response.proposal is not None
    assert response.validation.valid is False
    assert "PROTECTED_OBJECT" in issue_codes(response)
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned-a").segments] == [
        (36, 37)
    ]
    assert response.proposal.operations == []


@pytest.mark.parametrize(
    ("text", "authorization_text", "start_slot"),
    [
        ("移动 A 到 10:00", "请移动 A", 36),
        ("重排整个下午", "重排整个下午", 36),
    ],
)
def test_protected_authorization_rejects_non_verbatim_or_out_of_scope_evidence(
    text: str,
    authorization_text: str,
    start_slot: int,
) -> None:
    request = request_with_items(
        [internal_item("pinned-a", "A", 1, [(start_slot, start_slot + 1)], pinned=True)],
        text=text,
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "pinned-a",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 60},
                    "inputOrder": 0,
                    "authorizationText": authorization_text,
                }
            ]
        ),
    )

    assert response.validation.valid is False
    assert "PROTECTED_OBJECT" in issue_codes(response)


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


@pytest.mark.parametrize(
    ("model_slot", "request_text"),
    [
        pytest.param(74, "写完报告之后跑步", id="correct-model-anchor"),
        pytest.param(84, "写完报告之后跑步", id="wrong-model-anchor"),
        pytest.param(84, "写完报告之后，跑步", id="punctuated-wrong-model-anchor"),
    ],
)
def test_explicit_add_cascades_following_tasks_around_pinned_and_external_blocks(
    model_slot: int,
    request_text: str,
) -> None:
    temporary_id = UUID("77777777-7777-4777-8777-777777777777")
    request = request_with_items(
        [
            internal_item("report", "写报告", 3, [(71, 74)]),
            internal_item("game", "玩游戏", 3, [(74, 77)]),
            internal_item("pinned", "钉住任务", 2, [(77, 79)], pinned=True),
            internal_item("shower", "洗澡", 2, [(79, 81)]),
            external_item("external", "外部会议", 2, [(82, 84)]),
        ],
        now="2026-08-24T15:54:00+08:00",
        text=request_text,
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "跑步",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": model_slot},
                    "inputOrder": 0,
                }
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "report").segments] == [
        (71, 74)
    ]
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(74, 76)]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "game").segments] == [
        (76, 77),
        (79, 81),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned").segments] == [
        (77, 79)
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "shower").segments] == [
        (81, 82),
        (84, 85),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "external").segments] == [
        (82, 84)
    ]
    assert [operation.type for operation in response.proposal.operations] == [
        "add",
        "move",
        "move",
    ]


def test_clear_after_intent_discards_model_derived_move_before_cascade() -> None:
    temporary_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    request = request_with_items(
        [
            internal_item("report", "写报告", 3, [(71, 74)]),
            internal_item("game", "玩游戏", 3, [(74, 77)]),
            internal_item("pinned", "钉住任务", 2, [(77, 79)], pinned=True),
            internal_item("shower", "洗澡", 2, [(79, 81)]),
            external_item("external", "外部会议", 2, [(82, 84)]),
        ],
        now="2026-08-24T15:54:00+08:00",
        text="写完报告之后跑步",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "跑步",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": 84},
                    "inputOrder": 0,
                },
                {
                    "type": "move",
                    "targetItemId": "game",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 84},
                    "inputOrder": 1,
                },
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "report").segments] == [
        (71, 74)
    ]
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(74, 76)]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "game").segments] == [
        (76, 77),
        (79, 81),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "pinned").segments] == [
        (77, 79)
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "shower").segments] == [
        (81, 82),
        (84, 85),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "external").segments] == [
        (82, 84)
    ]
    assert [operation.type for operation in response.proposal.operations] == [
        "add",
        "move",
        "move",
    ]


def test_clear_after_intent_preserves_explicit_move_of_named_task() -> None:
    temporary_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    request = request_with_items(
        [
            internal_item("report", "写报告", 3, [(71, 74)]),
            internal_item("game", "玩游戏", 3, [(74, 77)]),
            internal_item("pinned", "钉住任务", 2, [(77, 79)], pinned=True),
            internal_item("shower", "洗澡", 2, [(79, 81)]),
            external_item("external", "外部会议", 2, [(82, 84)]),
        ],
        now="2026-08-24T15:54:00+08:00",
        text="写完报告之后跑步，把玩游戏移到21:00",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "跑步",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": 84},
                    "inputOrder": 0,
                },
                {
                    "type": "move",
                    "targetItemId": "game",
                    "allowedChanges": ["segments"],
                    "placement": {"anchor": "start", "slot": 84},
                    "inputOrder": 1,
                    "authorizationText": "把玩游戏移到21:00",
                },
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(74, 76)]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "game").segments] == [
        (84, 87)
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "shower").segments] == [
        (79, 81)
    ]
    assert [operation.type for operation in response.proposal.operations] == [
        "add",
        "move",
    ]


def test_explicit_add_preserves_following_task_when_existing_gap_is_sufficient() -> None:
    temporary_id = UUID("88888888-8888-4888-8888-888888888888")
    request = request_with_items(
        [
            internal_item("report", "写报告", 3, [(71, 74)]),
            internal_item("game", "玩游戏", 2, [(76, 78)]),
        ],
        now="2026-08-24T15:54:00+08:00",
        text="写完报告之后跑步",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "跑步",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": 74},
                    "inputOrder": 0,
                }
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(74, 76)]
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "game").segments] == [
        (76, 78)
    ]
    assert [operation.type for operation in response.proposal.operations] == ["add"]


def test_explicit_time_after_relation_keeps_model_anchor() -> None:
    temporary_id = UUID("99999999-9999-4999-8999-999999999999")
    request = request_with_items(
        [internal_item("report", "写报告", 3, [(71, 74)])],
        now="2026-08-24T15:54:00+08:00",
        text="写完报告之后，21:00 跑步",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "跑步",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": 84},
                    "inputOrder": 0,
                }
            ]
        ),
        uuid_factory=lambda: temporary_id,
    )

    assert response.validation.valid is True
    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(84, 86)]


def test_delete_operations_drive_typed_explicit_sets_and_exact_candidate_id_set() -> None:
    request = request_with_items(
        [
            internal_item("occurrence-delete", "删除内部", 2, [(36, 38)]),
            external_item("external-delete", "删除外部", 2, [(40, 42)]),
            internal_item("occurrence-keep", "保留", 2, [(44, 46)]),
        ],
        text="删除内部和删除外部",
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
                    "authorizationText": "删除外部",
                },
            ]
        ),
    )

    assert response.validation.valid is True
    assert response.proposal is not None
    assert response.proposal.deleted_occurrence_ids == ["occurrence-delete"]
    assert response.proposal.deleted_external_event_ids == ["external-delete"]
    assert [item.item_id for item in response.proposal.candidate_plan.items] == ["occurrence-keep"]


def test_next_revision_delete_of_temporary_task_has_no_real_domain_deletion_id() -> None:
    temporary_id = UUID("99999999-9999-4999-8999-999999999999")
    first_response = plan_time_fragment(
        request_with_items([], text="新增临时任务"),
        model_output([{"type": "add", "title": "临时任务", "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )
    assert first_response.proposal is not None
    next_request = TimeFragmentPlanRequestV2.model_validate(
        {
            "text": "删除临时任务",
            "requestID": "request-delete-temporary",
            "baseFingerprint": "sha256:planner-base",
            "currentPlan": first_response.proposal.candidate_plan.model_dump(
                mode="json",
                by_alias=True,
            ),
            "now": "2026-08-24T08:00:00+08:00",
        }
    )

    second_response = plan_time_fragment(
        next_request,
        model_output(
            [
                {
                    "type": "delete",
                    "targetItemId": str(temporary_id),
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert second_response.validation.valid is True
    assert second_response.proposal is not None
    assert second_response.proposal.candidate_plan.items == []
    assert second_response.proposal.deleted_occurrence_ids == []
    assert second_response.proposal.deleted_external_event_ids == []


def test_change_duration_preserves_past_start_without_a_move_operation() -> None:
    request = request_with_items(
        [internal_item("past", "晨间任务", 2, [(32, 34)])],
        now="2026-08-24T12:00:00+08:00",
        text="把晨间任务改成一小时",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "changeDuration",
                    "targetItemId": "past",
                    "allowedChanges": ["durationSlots", "segments"],
                    "durationSlots": 4,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "past").segments] == [
        (32, 36)
    ]


def test_change_duration_at_past_anchor_does_not_fall_forward_when_capacity_is_insufficient() -> None:
    request = request_with_items(
        [
            internal_item("past", "晨间任务", 2, [(32, 34)]),
            external_item("fixed", "后续固定", 62, [(34, 96)]),
        ],
        now="2026-08-24T12:00:00+08:00",
        text="把晨间任务改成一小时",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "changeDuration",
                    "targetItemId": "past",
                    "allowedChanges": ["durationSlots", "segments"],
                    "durationSlots": 4,
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert item_by_id(response, "past").segments == []
    assert "UNPLACED" in issue_codes(response)


def test_past_task_moves_after_now_only_when_a_move_operation_targets_it() -> None:
    request = request_with_items(
        [internal_item("past", "晨间任务", 2, [(32, 34)])],
        now="2026-08-24T12:00:00+08:00",
        text="把晨间任务移到现在之后",
    )

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "move",
                    "targetItemId": "past",
                    "allowedChanges": ["segments"],
                    "inputOrder": 0,
                }
            ]
        ),
    )

    assert response.validation.valid is True
    assert [(segment.start_slot, segment.end_slot) for segment in item_by_id(response, "past").segments] == [
        (48, 50)
    ]


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


def test_global_solver_reassigns_anchored_tasks_instead_of_false_unplaced() -> None:
    ids: Iterator[UUID] = iter(
        [
            UUID("77777777-7777-4777-8777-777777777777"),
            UUID("88888888-8888-4888-8888-888888888888"),
        ]
    )
    request = request_with_items([external_item("fixed", "固定占用", 60, [(36, 96)])])

    response = plan_time_fragment(
        request,
        model_output(
            [
                {
                    "type": "add",
                    "title": "从八点开始",
                    "durationSlots": 2,
                    "placement": {"anchor": "start", "slot": 32},
                    "inputOrder": 0,
                },
                {
                    "type": "add",
                    "title": "八点四十五前结束",
                    "durationSlots": 2,
                    "placement": {"anchor": "end", "slot": 35},
                    "inputOrder": 1,
                },
            ]
        ),
        uuid_factory=lambda: next(ids),
    )

    first = item_by_id(response, "77777777-7777-4777-8777-777777777777")
    second = item_by_id(response, "88888888-8888-4888-8888-888888888888")
    assert [(segment.start_slot, segment.end_slot) for segment in first.segments] == [
        (32, 33),
        (35, 36),
    ]
    assert [(segment.start_slot, segment.end_slot) for segment in second.segments] == [(33, 35)]
    assert response.validation.valid is True
    assert "UNPLACED" not in issue_codes(response)


def test_solver_timeout_falls_back_to_deterministic_allocator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_id = UUID("99999999-9999-4999-8999-999999999999")
    monkeypatch.setattr("app.time_fragment.solve_slot_allocations", lambda *_: None)

    response = plan_time_fragment(
        request_with_items([]),
        model_output([{"type": "add", "title": "回退任务", "durationSlots": 2, "inputOrder": 0}]),
        uuid_factory=lambda: temporary_id,
    )

    assert [
        (segment.start_slot, segment.end_slot)
        for segment in item_by_id(response, str(temporary_id)).segments
    ] == [(32, 34)]
    assert response.validation.valid is True
    assert "UNPLACED" not in issue_codes(response)


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
