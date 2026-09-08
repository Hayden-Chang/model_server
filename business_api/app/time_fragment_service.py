import asyncio
import json
from dataclasses import replace
from datetime import date as Date
from datetime import datetime
from typing import Any

from .contracts import TimeFragmentPlanRequestV2, TimeFragmentPlanResponseV2
from .model_client import ModelGatewayUnavailable
from .pipelines import get_pipeline
from .time_fragment import (
    build_time_fragment_parse_failed_response,
    plan_time_fragment,
    project_time_fragment_request_for_model,
)
from .time_fragment_postprocessor import (
    TimeFragmentModelOutputInvalid,
    build_time_fragment_correction_input,
    parse_time_fragment_model_operations,
)


_PLAN_TIMEOUT_SECONDS = 45.0
_INITIAL_MODEL_TIMEOUT_SECONDS = 30.0
_CORRECTION_TIMEOUT_SECONDS = 15.0


class TimeFragmentInputTooLarge(Exception):
    pass


class TimeFragmentRequestInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def execute_time_fragment_plan(
    model_client: Any,
    request: TimeFragmentPlanRequestV2,
    *,
    max_input_chars: int,
) -> TimeFragmentPlanResponseV2:
    try:
        async with asyncio.timeout(_PLAN_TIMEOUT_SECONDS):
            return await _execute_time_fragment_plan(model_client, request, max_input_chars=max_input_chars)
    except TimeoutError as error:
        raise ModelGatewayUnavailable("model gateway request timed out") from error


async def _execute_time_fragment_plan(
    model_client: Any,
    request: TimeFragmentPlanRequestV2,
    *,
    max_input_chars: int,
) -> TimeFragmentPlanResponseV2:
    _validate_temporal_request(request)
    pipeline = get_pipeline("time-fragment-plan-v2")
    assert pipeline is not None
    pipeline = replace(pipeline, timeout_seconds=_INITIAL_MODEL_TIMEOUT_SECONDS)
    model_request = project_time_fragment_request_for_model(request)
    if request.earliest_start_slot is not None:
        slot = request.earliest_start_slot
        prefix = f"从 {slot // 4:02}:{slot % 4 * 15:02} 开始\n"
        if model_request.text.startswith(prefix):
            model_request = model_request.model_copy(update={"text": model_request.text[len(prefix):]})
    initial_input = json.dumps(
        model_request.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _ensure_input_within_limit(initial_input, max_input_chars)

    async with asyncio.timeout(_INITIAL_MODEL_TIMEOUT_SECONDS):
        first_output = await model_client.complete(pipeline, initial_input)
    try:
        first_operations = parse_time_fragment_model_operations(first_output.content)
    except TimeFragmentModelOutputInvalid as error:
        correction_input = build_time_fragment_correction_input(
            model_request,
            error.issues,
            None,
        )
    else:
        first_response = plan_time_fragment(request, first_operations, attempts=1)
        if first_response.validation.valid:
            return first_response
        correction_issues = [
            issue
            for issue in first_response.validation.issues
            if issue.severity == "error"
        ]
        if not correction_issues:
            return first_response
        correction_input = build_time_fragment_correction_input(
            model_request,
            correction_issues,
            first_response,
        )
        if first_operations.temporal_relations:
            with_extraction = build_time_fragment_correction_input(
                model_request, correction_issues, first_response, first_operations,
            )
            if len(with_extraction) <= max_input_chars:
                correction_input = with_extraction

    _ensure_input_within_limit(correction_input, max_input_chars)
    fallback_pipeline = replace(
        pipeline, thinking_mode="disabled", reasoning_effort=None, timeout_seconds=_CORRECTION_TIMEOUT_SECONDS,
    )
    async with asyncio.timeout(_CORRECTION_TIMEOUT_SECONDS):
        second_output = await model_client.complete(fallback_pipeline, correction_input)
    try:
        second_operations = parse_time_fragment_model_operations(second_output.content)
    except TimeFragmentModelOutputInvalid:
        return build_time_fragment_parse_failed_response(
            request.request_id,
            attempts=2,
        )
    return plan_time_fragment(request, second_operations, attempts=2)


def _ensure_input_within_limit(user_input: str, max_input_chars: int) -> None:
    if len(user_input) > max_input_chars:
        raise TimeFragmentInputTooLarge


def _validate_temporal_request(request: TimeFragmentPlanRequestV2) -> None:
    selected_date = Date.fromisoformat(request.current_plan.date)
    local_date = datetime.fromisoformat(request.now.replace("Z", "+00:00")).date()
    if selected_date < local_date:
        raise TimeFragmentRequestInvalid(
            "PLANNING_DATE_NOT_ALLOWED",
            "planning date cannot be before today",
        )
