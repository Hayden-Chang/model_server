from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    business_api_key: SecretStr = Field(min_length=16)
    litellm_base_url: HttpUrl = "http://litellm:4000"
    litellm_master_key: SecretStr = Field(min_length=16)
    litellm_model_alias: str = "primary-model"
    structured_output_mode: Literal["json_schema", "json_object"] = "json_schema"
    model_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    max_input_chars: int = Field(default=20_000, ge=1, le=200_000)
    time_fragment_token_secret: SecretStr = Field(min_length=32)
    time_fragment_token_ttl_seconds: int = Field(default=2_592_000, ge=300, le=31_536_000)
    time_fragment_guest_quota_limit: int = Field(default=50, ge=1, le=10_000)
    time_fragment_development_device_ids: str = ""
    admin_api_key: SecretStr | None = Field(default=None, min_length=16)
    usage_db_path: str = ":memory:"
    usage_content_retention_days: int = Field(default=30, ge=1, le=365)


@lru_cache
def get_settings() -> Settings:
    return Settings()
