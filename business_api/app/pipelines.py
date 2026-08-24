import json
from dataclasses import dataclass
from typing import Any


ANALYSIS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "key_points", "risks"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "key_points": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "risks": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}

TIME_FRAGMENT_PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 96,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "start", "end", "status"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": ["scheduled", "active", "done", "skipped"],
                    },
                },
            },
        }
    },
}


@dataclass(frozen=True)
class Pipeline:
    pipeline_id: str
    system_prompt: str
    temperature: float
    max_tokens: int
    response_schema: dict[str, Any] | None = None

    def messages(self, user_input: str) -> list[dict[str, str]]:
        system_prompt = self.system_prompt
        if self.response_schema is not None:
            schema = json.dumps(self.response_schema, ensure_ascii=False, separators=(",", ":"))
            system_prompt += (
                " Return only JSON that conforms to this JSON Schema; do not wrap it in Markdown: "
                + schema
            )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]


PIPELINES: dict[str, Pipeline] = {
    "general-text-v1": Pipeline(
        pipeline_id="general-text-v1",
        system_prompt="Answer the user's request accurately and concisely.",
        temperature=0.2,
        max_tokens=2_000,
    ),
    "general-analysis-v1": Pipeline(
        pipeline_id="general-analysis-v1",
        system_prompt="Analyze the user's input for a downstream business system.",
        temperature=0.1,
        max_tokens=2_000,
        response_schema=ANALYSIS_SCHEMA,
    ),
    "time-fragment-plan-v1": Pipeline(
        pipeline_id="time-fragment-plan-v1",
        system_prompt=(
            "You are the Time Fragment scheduling engine. The user message is a JSON object with "
            "text, currentPlan, and now. Interpret now as the user's local wall-clock time. Return "
            "only the required JSON object. Keep every scheduled task on the calendar date of now; "
            "emit local ISO 8601 datetimes without Z or a timezone suffix, aligned to 15-minute "
            "boundaries. A task may be left unscheduled only by setting both start and end to null. "
            "For a first request, create a practical non-overlapping day plan. When currentPlan is "
            "present, change only what text explicitly requests and preserve every unaffected task's "
            "id, title, start, end, and status exactly. Preserve done and skipped tasks unless the "
            "request explicitly addresses them. New tasks use stable short ids and scheduled status. "
            "Sort scheduled tasks by start time, place unscheduled tasks afterward, and never return "
            "overlapping or cross-day times."
        ),
        temperature=0.1,
        max_tokens=4_000,
        response_schema=TIME_FRAGMENT_PLAN_SCHEMA,
    ),
}


def get_pipeline(pipeline_id: str) -> Pipeline | None:
    return PIPELINES.get(pipeline_id)
