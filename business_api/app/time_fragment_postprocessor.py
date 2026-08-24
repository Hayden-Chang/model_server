import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .contracts import (
    TimeFragmentModelOperations,
    TimeFragmentModelPlanRequest,
    TimeFragmentPlanResponseV2,
    TimeFragmentValidationIssue,
)


@dataclass(frozen=True)
class TimeFragmentCorrectionIssue:
    code: str
    message: str


class TimeFragmentModelOutputInvalid(Exception):
    def __init__(self, issues: list[TimeFragmentCorrectionIssue]) -> None:
        super().__init__(issues[0].message)
        self.issues = issues


def parse_time_fragment_model_operations(content: str) -> TimeFragmentModelOperations:
    candidate = content.strip()
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    elif candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()

    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise TimeFragmentModelOutputInvalid(
            [TimeFragmentCorrectionIssue("PARSE_FAILED", "模型输出不是有效 JSON")]
        ) from error
    if not isinstance(value, dict):
        raise TimeFragmentModelOutputInvalid(
            [TimeFragmentCorrectionIssue("PARSE_FAILED", "模型输出必须是 JSON 对象")]
        )

    try:
        return TimeFragmentModelOperations.model_validate(value)
    except ValidationError as error:
        issues = [
            TimeFragmentCorrectionIssue(
                "PARSE_FAILED",
                f"模型输出字段 {_format_location(item['loc'])} 不符合 operations 结构（{item['type']}）",
            )
            for item in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        ]
        raise TimeFragmentModelOutputInvalid(issues) from error


def build_time_fragment_correction_input(
    original_request: TimeFragmentModelPlanRequest,
    issues: list[TimeFragmentCorrectionIssue | TimeFragmentValidationIssue],
    first_response: TimeFragmentPlanResponseV2 | None,
) -> str:
    correction: dict[str, Any] = {
        "instruction": "第一次 operations 未通过结构或语义校验。根据具体 issues 修正，并只返回完整替换后的 operations JSON。",
        "originalRequest": original_request.model_dump(mode="json", by_alias=True),
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                **(
                    {
                        "itemId": issue.item_id,
                        "field": issue.field,
                    }
                    if isinstance(issue, TimeFragmentValidationIssue)
                    else {}
                ),
            }
            for issue in issues
        ],
    }
    if first_response is not None:
        assert first_response.proposal is not None
        correction["firstCandidate"] = _remove_private_fields(
            first_response.proposal.model_dump(mode="json", by_alias=True)
        )
    return json.dumps(correction, ensure_ascii=False, separators=(",", ":"))


def _format_location(location: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in location) or "<root>"


def _remove_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        forbidden = {
            "authorizationText",
            "baseFingerprint",
            "domainRef",
            "requestID",
            "status",
        }
        return {
            key: _remove_private_fields(item)
            for key, item in value.items()
            if key not in forbidden
        }
    if isinstance(value, list):
        return [_remove_private_fields(item) for item in value]
    return value
