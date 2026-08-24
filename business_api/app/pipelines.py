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
}


def get_pipeline(pipeline_id: str) -> Pipeline | None:
    return PIPELINES.get(pipeline_id)

