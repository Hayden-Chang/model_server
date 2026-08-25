#!/usr/bin/env python3
"""Validate the non-sensitive response from the production Time Fragment smoke."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_PUBLIC_KEYS = {"status", "authorizationText", "isExplicit"}
ALGORITHM_VERSION = "time-fragment-planner-v1"
EXPECTED_SMOKE_TITLE = "Production Smoke"


class SmokeValidationError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeValidationError(message)


def require_object(value: Any, path: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{path} must be an object")
    return value


def require_array(value: Any, path: str) -> list[Any]:
    require(isinstance(value, list), f"{path} must be an array")
    return value


def require_string(value: Any, path: str) -> str:
    require(isinstance(value, str) and bool(value), f"{path} must be a non-empty string")
    return value


def require_int(value: Any, path: str) -> int:
    require(isinstance(value, int) and not isinstance(value, bool), f"{path} must be an integer")
    return value


def find_forbidden_keys(value: Any, path: str = "response") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_PUBLIC_KEYS:
                found.append(child_path)
            found.extend(find_forbidden_keys(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_forbidden_keys(item, f"{path}[{index}]"))
    return found


def unique_string_set(value: Any, path: str) -> set[str]:
    items = require_array(value, path)
    strings = [require_string(item, f"{path}[{index}]") for index, item in enumerate(items)]
    require(len(strings) == len(set(strings)), f"{path} must not contain duplicate IDs")
    return set(strings)


def validate_item(item: Any, path: str, occupied_slots: dict[int, str]) -> dict[str, Any]:
    candidate = require_object(item, path)
    item_id = require_string(candidate.get("itemId"), f"{path}.itemId")
    object_type = candidate.get("objectType")
    require(object_type in {"internalTask", "externalEvent"}, f"{path}.objectType is invalid")
    require_string(candidate.get("title"), f"{path}.title")
    duration_slots = require_int(candidate.get("durationSlots"), f"{path}.durationSlots")
    require(1 <= duration_slots <= 96, f"{path}.durationSlots must be within 1...96")
    require("domainRef" in candidate, f"{path}.domainRef is required")

    if object_type == "internalTask":
        domain_ref = candidate["domainRef"]
        if domain_ref is not None:
            internal_ref = require_object(domain_ref, f"{path}.domainRef")
            require_string(internal_ref.get("taskId"), f"{path}.domainRef.taskId")
            require(
                internal_ref.get("occurrenceId") == item_id,
                f"{path}.domainRef.occurrenceId must match itemId",
            )
        require(isinstance(candidate.get("isPinned"), bool), f"{path}.isPinned must be boolean")
        require(
            isinstance(candidate.get("isCompleted"), bool),
            f"{path}.isCompleted must be boolean",
        )
    else:
        external_ref = require_object(candidate["domainRef"], f"{path}.domainRef")
        require(
            external_ref.get("externalEventId") == item_id,
            f"{path}.domainRef.externalEventId must match itemId",
        )
        require(isinstance(candidate.get("isAllDay"), bool), f"{path}.isAllDay must be boolean")
        require(isinstance(candidate.get("isFixed"), bool), f"{path}.isFixed must be boolean")

    segments = require_array(candidate.get("segments"), f"{path}.segments")
    total_duration = 0
    previous_end = -1
    for index, raw_segment in enumerate(segments):
        segment_path = f"{path}.segments[{index}]"
        segment = require_object(raw_segment, segment_path)
        start_slot = require_int(segment.get("startSlot"), f"{segment_path}.startSlot")
        end_slot = require_int(segment.get("endSlot"), f"{segment_path}.endSlot")
        require(0 <= start_slot < end_slot <= 96, f"{segment_path} must stay within 0...96")
        require(start_slot >= previous_end, f"{path}.segments must be sorted and non-overlapping")
        previous_end = end_slot
        total_duration += end_slot - start_slot
        for slot in range(start_slot, end_slot):
            owner = occupied_slots.get(slot)
            require(owner is None, f"candidatePlan items {owner} and {item_id} overlap at slot {slot}")
            occupied_slots[slot] = item_id

    if segments:
        require(
            total_duration == duration_slots,
            f"{path}.segments total must equal durationSlots",
        )
    return candidate


def validate_response(
    response: Any,
    expected_request_id: str,
    expected_fingerprint: str,
    expected_date: str,
) -> tuple[int, int]:
    root = require_object(response, "response")
    forbidden = find_forbidden_keys(root)
    require(not forbidden, "public response contains forbidden keys: " + ", ".join(forbidden))
    require(root.get("requestID") == expected_request_id, "requestID was not echoed exactly")

    proposal = require_object(root.get("proposal"), "response.proposal")
    require(
        proposal.get("baseFingerprint") == expected_fingerprint,
        "proposal.baseFingerprint was not echoed exactly",
    )
    require(
        proposal.get("algorithmVersion") == ALGORITHM_VERSION,
        f"proposal.algorithmVersion must be {ALGORITHM_VERSION}",
    )

    validation = require_object(root.get("validation"), "response.validation")
    attempts = require_int(validation.get("attempts"), "response.validation.attempts")
    require(attempts in {1, 2}, "response.validation.attempts must be 1 or 2")
    require(validation.get("valid") is True, "production smoke proposal must be valid")
    issues = require_array(validation.get("issues"), "response.validation.issues")
    require(
        not any(isinstance(issue, dict) and issue.get("severity") == "error" for issue in issues),
        "production smoke response contains a validation error",
    )

    operations = require_array(proposal.get("operations"), "response.proposal.operations")
    add_operations: list[dict[str, Any]] = []
    delete_operations: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(operations):
        operation_path = f"response.proposal.operations[{index}]"
        operation = require_object(raw_operation, operation_path)
        operation_type = operation.get("type")
        require(
            operation_type in {"add", "move", "changeDuration", "changeTitle", "delete"},
            f"{operation_path}.type is invalid",
        )
        if operation_type == "add":
            add_operations.append(operation)
        elif operation_type == "delete":
            delete_operations.append(operation)

    require(len(add_operations) == 1, "smoke request must produce exactly one add operation")
    add_operation = add_operations[0]
    temporary_id = require_string(
        add_operation.get("temporaryId"),
        "response.proposal.operations[add].temporaryId",
    )
    add_title = require_string(
        add_operation.get("title"),
        "response.proposal.operations[add].title",
    )
    require(
        add_title == EXPECTED_SMOKE_TITLE,
        f"smoke add title must be {EXPECTED_SMOKE_TITLE}",
    )
    require(
        require_int(
            add_operation.get("durationSlots"),
            "response.proposal.operations[add].durationSlots",
        )
        == 2,
        "default 30-minute task must use two 15-minute slots",
    )

    deleted_occurrences = unique_string_set(
        proposal.get("deletedOccurrenceIDs"),
        "response.proposal.deletedOccurrenceIDs",
    )
    deleted_external_events = unique_string_set(
        proposal.get("deletedExternalEventIDs"),
        "response.proposal.deletedExternalEventIDs",
    )
    delete_occurrences: set[str] = set()
    delete_external_events: set[str] = set()
    for index, operation in enumerate(delete_operations):
        target = require_string(
            operation.get("targetItemId"),
            f"response.proposal.deleteOperations[{index}].targetItemId",
        )
        object_type = operation.get("objectType")
        require(
            object_type in {"internalTask", "externalEvent"},
            f"response.proposal.deleteOperations[{index}].objectType is invalid",
        )
        target_set = delete_occurrences if object_type == "internalTask" else delete_external_events
        require(target not in target_set, "delete operations must not repeat a target")
        target_set.add(target)
    require(
        deleted_occurrences == delete_occurrences,
        "deletedOccurrenceIDs must match internalTask delete operations",
    )
    require(
        deleted_external_events == delete_external_events,
        "deletedExternalEventIDs must match externalEvent delete operations",
    )

    candidate_plan = require_object(proposal.get("candidatePlan"), "response.proposal.candidatePlan")
    require(candidate_plan.get("date") == expected_date, "candidatePlan.date is not today's date")
    raw_items = require_array(candidate_plan.get("items"), "response.proposal.candidatePlan.items")
    require(raw_items, "candidatePlan.items must contain the requested smoke task")
    occupied_slots: dict[int, str] = {}
    items = [
        validate_item(item, f"response.proposal.candidatePlan.items[{index}]", occupied_slots)
        for index, item in enumerate(raw_items)
    ]
    candidate_ids = [item["itemId"] for item in items]
    require(len(candidate_ids) == len(set(candidate_ids)), "candidatePlan item IDs must be unique")
    add_ids = {
        require_string(operation.get("temporaryId"), "add operation temporaryId")
        for operation in add_operations
    }
    require(
        set(candidate_ids) == add_ids,
        "empty-baseline candidate IDs must equal the add-operation temporary IDs",
    )

    smoke_item = next(item for item in items if item["itemId"] == temporary_id)
    require(smoke_item.get("objectType") == "internalTask", "smoke task must be an internalTask")
    require(smoke_item.get("domainRef") is None, "new smoke task domainRef must be null")
    require(
        smoke_item.get("title") == EXPECTED_SMOKE_TITLE,
        f"smoke task title must be {EXPECTED_SMOKE_TITLE}",
    )
    require(smoke_item.get("durationSlots") == 2, "smoke task duration must be 30 minutes")
    require(smoke_item.get("segments"), "empty-day smoke task must be completely scheduled")
    return attempts, len(items)


def load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("response", help="response JSON path, or - for stdin")
    parser.add_argument("expected_request_id")
    parser.add_argument("expected_fingerprint")
    parser.add_argument("expected_date")
    args = parser.parse_args()

    try:
        response = load_json(args.response)
        attempts, item_count = validate_response(
            response,
            args.expected_request_id,
            args.expected_fingerprint,
            args.expected_date,
        )
    except (OSError, json.JSONDecodeError, SmokeValidationError) as error:
        print(f"Time Fragment V2 smoke validation failed: {error}", file=sys.stderr)
        return 1

    print(
        "Time Fragment V2 smoke passed: "
        f"date={args.expected_date} attempts={attempts} candidateItems={item_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
