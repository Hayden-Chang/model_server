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
    model_alias: str | None = None
    response_schema: dict[str, Any] | None = None
    thinking_mode: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["low", "high", "max"] | None = None
    timeout_seconds: float | None = None

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
            "Understand the user's complete scheduling intent, including continuous activities and "
            "dependencies, then express it as structured operations and temporalRelations. "
            "Return add, move, changeDuration, changeTitle, or delete operations; never return a candidate "
            "task list, time fragments, domain references, lifecycle fields, or any real domain "
            "ID for a new task. "
            "For add, omit temporaryId because the service injects a UUID after parsing. Resolve "
            "explicit ranges, durations, and activity relationships before choosing a default duration. "
            "Use durationSlots=2 only for an otherwise unbounded activity; it must not truncate an "
            "activity that continues until the next one. Existing targets must use an exact "
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
            "For every existing-task move or changeDuration with explicit clocks, include authorizationText "
            "as an exact affirmative quote containing the requested clocks, even for an unpinned task. "
            "A start-to-end range requires move to the start slot and changeDuration for the full range. "
            "For add, never output placement or authorizationText. Always include sourceText as an exact "
            "task quote and timeConstraint explicitly as null or an object. The display title may summarize "
            "or combine source actions and need not occur verbatim in sourceText. A timed object requires "
            "startTime, endTime, startEvidence, endEvidence; an absent boundary and its evidence are both null. "
            "Use local 24-hour HH:mm clocks on currentPlan.date at their original minute precision, not slots; "
            "each non-null clock needs an exact original quote containing that one clock. startEvidence "
            "must refer to the task's sourceText; endEvidence may quote the next action or journey endpoint. "
            "Never compute an unstated boundary from duration. If only a start and duration are stated, "
            "set endTime and endEvidence to null and encode duration in durationSlots; if only an end and "
            "duration are stated, set startTime and startEvidence to null. For 在 13:00 插入一个 30 分钟的电话, "
            "use startTime=13:00, endTime=null, startEvidence=13:00, endEvidence=null, durationSlots=2; "
            "the solver computes the finish time and shifts affected tasks. "
            "Resolve omitted AM/PM from the whole narrative, not from now. For example 8:50 起床 is 08:50; "
            "12点午饭 followed by 1点上班 means 12:00 then 13:00. Midnight after 23:00 is 24:00, never noon "
            "or 00:00 on the same date. A global earliestStartSlot, "
            "relative ordering, or priority rule does not authorize a fabricated per-task clock. "
            "earliestStartSlot (or now when omitted for today) is a default, not a veto on explicit "
            "user times. Preserve an affirmative task clock even when it is earlier than that default "
            "or already past today. The service lowers this proposal's planning floor accordingly, "
            "also allowing untimed tasks to start earlier; do not shift, drop, or invent clocks to fit "
            "the default. Existing task protection, collisions and calendar-day bounds still apply. "
            "Represent relative ordering in temporalRelations even when timeConstraint is null. "
            "Use null only when no affirmative task clock is stated; do not omit explicit clocks or tasks. "
            "Keep the full timing context in each sourceText: one source clock may be both the previous "
            "journey's end and the following activity's start, with the same HH:mm interpretation. "
            "A clock is not consumed by its first use; overlapping sourceText and evidence are allowed. "
            "For 七点半下班8 点到家给吃晚饭 in an evening narrative, 下班回家 is 19:30–20:00 and "
            "吃晚饭 starts at 20:00: quote 8 点到家给吃晚饭 as its sourceText and 8 点到家 as startEvidence. "
            "Do not crop that meal to 给吃晚饭 with timeConstraint=null. In contrast, 有空再, 稍后, "
            "or 到家后 without an explicit start only express flexibility or relative order; do not "
            "automatically copy the preceding clock to them or to independent untimed tasks. "
            "Preserve source "
            "appearance in inputOrder. An explicit start-to-end range supplies both time boundaries; "
            "handle every range independently even when multiple tasks share one line or only punctuation "
            "separates them. For continuous activities in a chronological narrative, the next activity's start "
            "ends the current activity unless a stated duration, end, journey endpoint, or explicit gap "
            "says otherwise. Emit an until relation for that continuity. Endpoint phrases such as 出地铁 "
            "and 到家 describe the preceding "
            "journey and are not separate adds; 下班 followed by 到家 is one 下班回家 add. Preserve gaps "
            "after endpoint phrases, keep an otherwise unbounded 吃饭 at the default 2 slots. "
            "Always return temporalRelations, using [] only after checking that no inter-task "
            "relationship is needed. Each relation references two distinct new adds by their unique "
            "inputOrder: beforeInputOrder, afterInputOrder, kind, evidence. evidence is an exact "
            "affirmative original passage supporting the relationship. Task identity comes from "
            "inputOrder, not phrase matching: a quote such as 吃完以后回家 or 然后归档 may refer "
            "to its predecessor through context and need only overlap one of the linked tasks. Do not expand "
            "or alter task sourceText merely to fit a relation quote. kind=before "
            "means the first activity must finish before the second starts; it allows a gap. Use it "
            "for 然后, 吃完以后, and equivalent sequencing, including untimed activities between clocked "
            "ones. kind=until means a continuing activity lasts until the next activity starts; both "
            "the boundary must have a source-backed first endTime or second startTime. If both starts "
            "are given, until derives the first duration from them; if only the first end is given, "
            "until starts the next activity there without fabricating a source clock. Do not use until to override a "
            "stated shorter duration, an explicit ending, or an independent task. For example "
            "09:00 工作，11:30 午间休息，13:30 继续工作 has until links 工作→午间休息→继续工作. "
            "For 工作到18:00，然后用餐，用餐以后乘车返程，19:30 整理物品, preserve before links "
            "工作→用餐→乘车返程→整理物品; leave unstated meal and journey clocks null. Their times "
            "are computed by the service, not fabricated as quoted clocks. Independent tasks, priority "
            "rankings, or mere list order must not create dependencies. Never put an untimed task "
            "with an explicit predecessor into the independent [] case. Before returning, check that "
            "all continuous ranges and relative sequences in the narrative have been expressed. "
            "When the same clock appears twice, quote enough adjacent action text to uniquely identify "
            "each clock occurrence. Keep sourceText focused on its own activity; do not append the next "
            "task just to support a relation. In correction requests retain every already correct "
            "range and relation from firstExtraction; never replace until with before merely to bypass an error. "
            'Example input: 9点工作，12点休息，然后买菜。 Example JSON: '
            '{"operations":[{"type":"add","title":"工作","inputOrder":0,"durationSlots":2,'
            '"sourceText":"9点工作","timeConstraint":{"startTime":"09:00","endTime":null,'
            '"startEvidence":"9点工作","endEvidence":null}},'
            '{"type":"add","title":"休息","inputOrder":1,"durationSlots":2,'
            '"sourceText":"12点休息","timeConstraint":{"startTime":"12:00","endTime":null,'
            '"startEvidence":"12点休息","endEvidence":null}},'
            '{"type":"add","title":"买菜","inputOrder":2,"durationSlots":2,'
            '"sourceText":"然后买菜","timeConstraint":null}],'
            '"temporalRelations":[{"beforeInputOrder":0,"afterInputOrder":1,"kind":"until",'
            '"evidence":"9点工作，12点休息"},{"beforeInputOrder":1,"afterInputOrder":2,'
            '"kind":"before","evidence":"然后买菜"}]}. '
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
