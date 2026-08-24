from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)

    @field_validator("input")
    @classmethod
    def input_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("input must not be blank")
        return value


class ModelMetadata(BaseModel):
    alias: str
    provider_model: str | None = None
    usage: dict[str, Any] | None = None


class RunResponse(BaseModel):
    pipeline: str
    request_id: str
    result: str | dict[str, Any]
    model: ModelMetadata

