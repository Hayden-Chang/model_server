import json
from typing import Any

from .contracts import TimeFragmentPlanRequestV2, TimeFragmentPlanResponseV2
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


async def execute_time_fragment_plan(
    model_client: Any,
    request: TimeFragmentPlanRequestV2,
) -> TimeFragmentPlanResponseV2:
    pipeline = get_pipeline("time-fragment-plan-v2")
    assert pipeline is not None
    model_request = project_time_fragment_request_for_model(request)
    initial_input = json.dumps(
        model_request.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )

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
        correction_input = build_time_fragment_correction_input(
            model_request,
            first_response.validation.issues,
            first_response,
        )

    second_output = await model_client.complete(pipeline, correction_input)
    try:
        second_operations = parse_time_fragment_model_operations(second_output.content)
    except TimeFragmentModelOutputInvalid:
        return build_time_fragment_parse_failed_response(
            request.request_id,
            attempts=2,
        )
    return plan_time_fragment(request, second_operations, attempts=2)
