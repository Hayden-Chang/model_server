from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RentRoomSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RENT_ROOM_", env_file=".env", extra="ignore")

    app_id: str = "wxf43c7f280c98efca"
    app_secret: SecretStr | None = None
    offer_id: str = "1450681249"
    app_key: SecretStr | None = None
    token_secret: SecretStr | None = Field(default=None, min_length=32)
    push_token: SecretStr | None = Field(default=None, min_length=8)
    push_aes_key: SecretStr | None = Field(default=None, min_length=43, max_length=43)
    payments_enabled: bool = False
    ios_payments_enabled: bool = False
    free_capacity: int = Field(default=1, ge=0, le=9007199254740991)
    db_path: str = ":memory:"
    ios_bundle_id: str = ""
    apple_login_enabled: bool = False

    @field_validator("app_secret", "app_key", "token_secret", "push_token", "push_aes_key", mode="before")
    @classmethod
    def empty_secrets_are_disabled(cls, value):
        return None if value == "" else value

    @property
    def login_ready(self) -> bool:
        return bool(self.app_id and self.app_secret and self.token_secret)

    @property
    def apple_ready(self) -> bool:
        return bool(self.apple_login_enabled and self.token_secret and self.ios_bundle_id.strip())

    @property
    def payment_configured(self) -> bool:
        return bool(self.login_ready and self.offer_id
                    and self.app_key and self.push_token and self.push_aes_key)

    @property
    def payment_ready(self) -> bool:
        return self.payments_enabled and self.payment_configured

    @model_validator(mode="after")
    def validate_payment_config(self):
        if self.apple_login_enabled and not self.apple_ready:
            raise ValueError("Apple login requires a session secret and the exact native bundle ID")
        if self.payments_enabled and not self.payment_ready:
            raise ValueError("Rent-room payments require independent login, signing and secure push configuration")
        if self.token_secret and self.app_key and self.token_secret == self.app_key:
            raise ValueError("Rent-room session encryption and payment secrets must be independent")
        return self
