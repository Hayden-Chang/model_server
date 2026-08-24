import json
from typing import Any

from jsonschema import Draft202012Validator


class ModelOutputInvalid(Exception):
    pass


def process_text(content: str) -> str:
    result = content.strip()
    if not result:
        raise ModelOutputInvalid("model returned empty text")
    return result


def process_structured(content: str, schema: dict[str, Any]) -> dict[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    elif candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()

    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ModelOutputInvalid("model output is not valid JSON") from error

    if not isinstance(value, dict):
        raise ModelOutputInvalid("model output must be a JSON object")

    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise ModelOutputInvalid("model output does not match the pipeline schema")
    return value

