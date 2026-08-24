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
    "cross-day-boundary-todo",
    "delete-existing-internal",
    "delete-temporary",
    "empty-day-default-duration",
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
    now: str = "2026-08-24T08:00:00+08:00",
    text: str = "调整今天的计划",
) -> TimeFragmentPlanRequestV2:
    return TimeFragmentPlanRequestV2.model_validate(
        {
            "text": text,
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
                }
            ]
        ),
    )

    assert "UNKNOWN_TARGET" in issue_codes(response)
    retained = item_by_id(response, "external-1")
    assert [(segment.start_slot, segment.end_slot) for segment in retained.segments] == [(40, 42)]


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
