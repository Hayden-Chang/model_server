import json
from dataclasses import dataclass
from typing import Any, Literal

from .contracts import TimeFragmentExtractedOperations


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
                "required": ["id", "title", "start", "end"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                },
            },
        }
    },
}

TIME_FRAGMENT_OPERATIONS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **TimeFragmentExtractedOperations.model_json_schema(by_alias=True),
}


@dataclass(frozen=True)
class Pipeline:
    pipeline_id: str
    system_prompt: str
    temperature: float
    max_tokens: int
    response_schema: dict[str, Any] | None = None
    thinking_mode: Literal["enabled", "disabled"] | None = None

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
            "currentPlan always contains the current-day base, including an empty task list when the "
            "day has no tasks. Change only what text explicitly requests and preserve every unaffected task's "
            "id, title, start, and end exactly. New tasks use stable short ids. "
            "Sort scheduled tasks by start time, place unscheduled tasks afterward, and never return "
            "overlapping or cross-day times."
        ),
        temperature=0.1,
        max_tokens=4_000,
        response_schema=TIME_FRAGMENT_PLAN_SCHEMA,
    ),
    "time-fragment-plan-v2": Pipeline(
        pipeline_id="time-fragment-plan-v2",
        system_prompt=(
            "You convert a Time Fragment planning request into structured operations only. "
            "Return add, move, changeDuration, changeTitle, or delete operations; never return a candidate "
            "task list, time fragments, domain references, lifecycle fields, or any real domain "
            "ID for a new task. "
            "For add, omit temporaryId because the service injects a UUID after parsing, and use "
            "durationSlots=2 when the user gives no duration. Existing targets must use an exact "
            "itemId from currentPlan and must not be guessed from a similar title. Set objectType "
            "when known. move may authorize only segments; changeDuration must authorize "
            "durationSlots and segments; changeTitle requires objectType=internalTask and may "
            "authorize only title; ExternalEvent titles are source facts and cannot change; delete "
            "authorizes no mutable fields. For a pinned, "
            "completed, or external-event target, include authorizationText as the shortest exact "
            "quote from the user's text that affirmatively requests the change and names the exact "
            "target or an explicit time range. Never paraphrase authorizationText and never use a "
            "negative or keep-unchanged phrase as authorization. Existing move placement slots are "
            "15-minute grid indices from midnight on currentPlan.date: HH:mm maps to floor((HH*60+mm+7)/15). "
            "For add, never output placement or authorizationText. Always include sourceText as an exact "
            "task quote and timeConstraint explicitly as null or an object. The display title may summarize "
            "or combine source actions and need not occur verbatim in sourceText. A timed object requires "
            "startTime, endTime, startEvidence, endEvidence; an absent boundary and its evidence are both null. "
            "Use local 24-hour HH:mm clocks on currentPlan.date at their original minute precision, not slots; "
            "each non-null clock needs an exact original quote containing that one clock. startEvidence "
            "must refer to the task's sourceText; endEvidence may quote the next action or journey endpoint. "
            "Resolve omitted AM/PM from the whole narrative, not from now. For example 8:50 起床 is 08:50; "
            "12点午饭 followed by 1点上班 means 12:00 then 13:00. Midnight after 23:00 is 24:00, never noon "
            "or 00:00 on the same date. A global earliestStartSlot, "
            "relative ordering, or priority rule does not authorize a per-task timeConstraint. "
            "Use null only when no affirmative task clock is stated; do not omit explicit clocks or tasks. "
            "Preserve source "
            "appearance in inputOrder. An explicit start-to-end range supplies both time boundaries; "
            "handle every range independently even when multiple tasks share one line or only punctuation "
            "separates them. For a chronological sequence of clocked actions, the next clock "
            "may end the current action. Endpoint phrases such as 出地铁 and 到家 describe the preceding "
            "journey and are not separate adds; 下班 followed by 到家 is one 下班回家 add. Preserve gaps "
            "after endpoint phrases, keep an otherwise unbounded 吃饭 at the default 2 slots. "
            "The service will round each stated boundary to the nearest 15-minute slot and compute range "
            "duration, overriding durationSlots. Explicit time ranges take precedence. "
            "Include priority only when the user specified one. priority is a "
            "positive score: a larger positive priority score means higher priority. Encode every requested "
            "precedence level in that score; equally ranked tasks use inputOrder."
        ),
        temperature=0.0,
        max_tokens=20_000,
        response_schema=TIME_FRAGMENT_OPERATIONS_SCHEMA,
        thinking_mode="disabled",
    ),
}


def get_pipeline(pipeline_id: str) -> Pipeline | None:
    return PIPELINES.get(pipeline_id)
