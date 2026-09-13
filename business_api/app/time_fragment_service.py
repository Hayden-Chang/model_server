import asyncio
import json
import re
from dataclasses import replace
from datetime import date as Date
from datetime import datetime
from typing import Any

from .contracts import (
    TimeFragmentExtractedAddOperation,
    TimeFragmentExtractedOperations,
    TimeFragmentPlanRequestV2,
    TimeFragmentPlanResponseV2,
)
from .model_client import ModelGatewayUnavailable
from .pipelines import get_pipeline
from .time_fragment import (
    build_time_fragment_parse_failed_response,
    plan_time_fragment,
    project_time_fragment_request_for_model,
)
from .time_fragment_postprocessor import (
    TimeFragmentCorrectionIssue,
    TimeFragmentModelOutputInvalid,
    build_time_fragment_correction_input,
    parse_time_fragment_model_operations,
)


_PLAN_TIMEOUT_SECONDS = 45.0
_INITIAL_MODEL_TIMEOUT_SECONDS = 30.0
_CORRECTION_TIMEOUT_SECONDS = 15.0
_BARE_TITLE_MAX_LENGTH = 20
_BARE_TITLE_COMMAND_PREFIX = re.compile(
    r"^(?:把|将|请|帮我|麻烦|删除|删掉|移除|取消|移动|移到|挪到|改到|调到|调整|"
    r"改标题|修改标题|重命名|改名|安排|新增|添加|插入|不要|保持)"
)
_BARE_TITLE_COMMAND_SUFFIX = re.compile(r"(?:不要了|不用了|删掉了?|删除了?|移除了?|取消了?)$")
_BARE_TITLE_SENTENCE_BREAK = re.compile(r"[\r\n，,。；;！？!?：:、]")
_BARE_TITLE_SCHEDULING_DETAIL = re.compile(
    r"\d|^(?:二十[一二三四]?|十[一二三四五六七八九]?|[零〇一二两三四五六七八九])\s*点|"
    r"今天|明天|后天|上午|中午|下午|晚上|凌晨|早上|清晨|"
    r"点钟|分钟|小时|刻钟|"
    r"然后|之后|以前|之前|以后|直到|接着|随后|先.+再"
)


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
    bare_title = _bare_task_title(request.text)
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
        if bare_title is not None and not _matches_bare_title_contract(
            first_operations, bare_title,
        ):
            correction_input = build_time_fragment_correction_input(
                model_request,
                [_bare_title_correction_issue()],
                None,
                first_operations,
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
            if first_operations.temporal_relations or any(
                issue.field == "timeConstraint" for issue in correction_issues
            ):
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
        if bare_title is not None:
            return _plan_bare_title(request, bare_title)
        return build_time_fragment_parse_failed_response(
            request.request_id,
            attempts=2,
        )
    if bare_title is not None and not _matches_bare_title_contract(
        second_operations, bare_title,
    ):
        return _plan_bare_title(request, bare_title)
    return plan_time_fragment(request, second_operations, attempts=2)


def _bare_task_title(text: str) -> str | None:
    title = text.strip()
    if not 1 <= len(title) <= _BARE_TITLE_MAX_LENGTH:
        return None
    if _BARE_TITLE_SENTENCE_BREAK.search(title):
        return None
    if _BARE_TITLE_COMMAND_PREFIX.search(title) or _BARE_TITLE_COMMAND_SUFFIX.search(title):
        return None
    if _BARE_TITLE_SCHEDULING_DETAIL.search(title):
        return None
    return title


def _matches_bare_title_contract(
    operations: TimeFragmentExtractedOperations,
    title: str,
) -> bool:
    if operations.temporal_relations or len(operations.operations) != 1:
        return False
    operation = operations.operations[0]
    return (
        isinstance(operation, TimeFragmentExtractedAddOperation)
        and operation.title == title
        and operation.source_text == title
        and operation.duration_slots == 2
        and operation.priority is None
        and operation.input_order == 0
        and operation.time_constraint is None
    )


def _bare_title_correction_issue() -> TimeFragmentCorrectionIssue:
    return TimeFragmentCorrectionIssue(
        "BARE_TITLE_MISMATCH",
        "纯任务标题必须原样返回唯一 add，使用默认 30 分钟且不添加时间、优先级或关系",
    )


def _plan_bare_title(
    request: TimeFragmentPlanRequestV2,
    title: str,
) -> TimeFragmentPlanResponseV2:
    operations = TimeFragmentExtractedOperations(
        operations=[
            TimeFragmentExtractedAddOperation(
                type="add",
                title=title,
                durationSlots=2,
                priority=None,
                inputOrder=0,
                sourceText=title,
                timeConstraint=None,
            )
        ],
        temporalRelations=[],
    )
    return plan_time_fragment(request, operations, attempts=2)


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
