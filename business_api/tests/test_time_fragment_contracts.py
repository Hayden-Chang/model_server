import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts import (
    TimeFragmentInternalTaskItem,
    TimeFragmentModelOperations,
    TimeFragmentPlanProposal,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanResponseV2,
)
from app.pipelines import TIME_FRAGMENT_OPERATIONS_SCHEMA, get_pipeline
from app.time_fragment import (
    build_time_fragment_parse_failed_response,
    project_time_fragment_request_for_model,
)


def app_request_payload() -> dict[str, Any]:
    return {
        "text": "把写方案移到下午，并保留会议",
        "requestID": "request-contract-1",
        "baseFingerprint": "sha256:contract-base",
        "currentPlan": {
            "date": "2026-08-24",
            "items": [
                {
                    "itemId": "occurrence-1",
                    "objectType": "internalTask",
                    "domainRef": {
                        "taskId": "private-task-id",
                        "occurrenceId": "occurrence-1",
                        "scheduledTaskId": "private-scheduled-id",
                    },
                    "title": "写方案",
                    "durationSlots": 4,
                    "segments": [{"startSlot": 36, "endSlot": 40}],
                    "isPinned": False,
                    "isCompleted": False,
                },
                {
                    "itemId": "external-1",
                    "objectType": "externalEvent",
                    "domainRef": {"externalEventId": "external-1"},
                    "title": "客户会议",
                    "durationSlots": 4,
                    "segments": [{"startSlot": 56, "endSlot": 60}],
                    "isAllDay": False,
                    "isFixed": True,
                },
            ],
        },
        "now": "2026-08-24T08:10:00+08:00",
    }


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names.update(_property_names(value))
    elif isinstance(schema, list):
        for value in schema:
            names.update(_property_names(value))
    return names


def test_v2_app_contract_covers_full_request_response_and_discriminated_items() -> None:
    request_schema = TimeFragmentPlanRequestV2.model_json_schema(by_alias=True)
    response_schema = TimeFragmentPlanResponseV2.model_json_schema(by_alias=True)
    proposal_schema = TimeFragmentPlanProposal.model_json_schema(by_alias=True)

    assert set(request_schema["properties"]) == {
        "text",
        "requestID",
        "baseFingerprint",
        "currentPlan",
        "now",
    }
    assert set(response_schema["properties"]) == {"requestID", "proposal", "validation"}
    assert {
        "baseFingerprint",
        "algorithmVersion",
        "deletedOccurrenceIDs",
        "deletedExternalEventIDs",
        "operations",
        "candidatePlan",
    } == set(proposal_schema["properties"])
    assert "status" not in _property_names(request_schema)
    assert "status" not in _property_names(response_schema)

    request = TimeFragmentPlanRequestV2.model_validate(app_request_payload())
    assert request.current_plan.items[0].object_type == "internalTask"
    assert request.current_plan.items[1].object_type == "externalEvent"


@pytest.mark.parametrize(
    ("item_index", "extra_field"),
    [
        (0, {"status": "scheduled"}),
        (0, {"isAllDay": False}),
        (0, {"isFixed": False}),
        (1, {"status": "scheduled"}),
        (1, {"isCompleted": False}),
    ],
)
def test_v2_items_reject_status_and_cross_object_read_only_facts(
    item_index: int,
    extra_field: dict[str, Any],
) -> None:
    payload = app_request_payload()
    payload["currentPlan"]["items"][item_index].update(extra_field)

    with pytest.raises(ValidationError):
        TimeFragmentPlanRequestV2.model_validate(payload)


def test_internal_task_rejects_external_event_fields_even_without_request_wrapper() -> None:
    item = app_request_payload()["currentPlan"]["items"][0]
    item["isAllDay"] = False

    with pytest.raises(ValidationError):
        TimeFragmentInternalTaskItem.model_validate(item)


def test_model_visible_projection_never_contains_domain_references_or_envelope_ids() -> None:
    request = TimeFragmentPlanRequestV2.model_validate(app_request_payload())

    projection = project_time_fragment_request_for_model(request)
    serialized = json.dumps(projection.model_dump(mode="json", by_alias=True), ensure_ascii=False)

    assert "domainRef" not in serialized
    assert "private-task-id" not in serialized
    assert "private-scheduled-id" not in serialized
    assert "request-contract-1" not in serialized
    assert "sha256:contract-base" not in serialized
    assert [item.item_id for item in projection.current_plan.items] == [
        "occurrence-1",
        "external-1",
    ]


@pytest.mark.parametrize(
    "forbidden_field",
    [
        {"segments": [{"startSlot": 40, "endSlot": 42}]},
        {"domainRef": {"taskId": "forged"}},
        {"temporaryId": "11111111-1111-4111-8111-111111111111"},
        {"status": "scheduled"},
    ],
)
def test_model_add_output_accepts_operations_only_and_rejects_server_or_domain_fields(
    forbidden_field: dict[str, Any],
) -> None:
    operation = {"type": "add", "title": "新任务", "inputOrder": 0, **forbidden_field}

    with pytest.raises(ValidationError):
        TimeFragmentModelOperations.model_validate({"operations": [operation]})


def test_v2_model_schema_and_pipeline_expose_only_structured_operations() -> None:
    pipeline = get_pipeline("time-fragment-plan-v2")

    assert pipeline is not None
    assert pipeline.response_schema == TIME_FRAGMENT_OPERATIONS_SCHEMA
    assert set(TIME_FRAGMENT_OPERATIONS_SCHEMA["properties"]) == {"operations"}
    operation_property_names = _property_names(TIME_FRAGMENT_OPERATIONS_SCHEMA)
    assert "domainRef" not in operation_property_names
    assert "segments" not in operation_property_names
    assert "temporaryId" not in operation_property_names
    assert "status" not in operation_property_names
    assert "status" not in pipeline.system_prompt.lower()
    assert get_pipeline("time-fragment-plan-v1") is not None


def test_model_operation_defaults_add_duration_to_two_slots_without_inventing_an_id() -> None:
    output = TimeFragmentModelOperations.model_validate(
        {"operations": [{"type": "add", "title": "新任务", "inputOrder": 0}]}
    )

    operation = output.operations[0]
    assert operation.duration_slots == 2
    assert "temporaryId" not in operation.model_dump(mode="json", by_alias=True)


def test_parse_failure_has_structured_issue_and_no_empty_candidate() -> None:
    response = build_time_fragment_parse_failed_response(
        "request-parse-failed",
        attempts=2,
    )

    assert response.proposal is None
    assert response.validation.valid is False
    assert response.validation.attempts == 2
    assert [issue.code for issue in response.validation.issues] == ["PARSE_FAILED"]
