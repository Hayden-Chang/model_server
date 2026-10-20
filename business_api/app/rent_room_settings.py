from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


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
    db_path: str = ":memory:"
    ios_bundle_id: str = ""
    apple_login_enabled: bool = False
    sms_login_enabled: bool = False
    auth_identity_secret: SecretStr | None = Field(default=None, min_length=32)
    sms_access_key_id: SecretStr | None = None
    sms_access_key_secret: SecretStr | None = None
    sms_sign_name: str = ""
    sms_template_code: str = ""
    sms_daily_limit: int = Field(default=1000, ge=1, le=100000)
    storekit_enabled: bool = False
    storekit_product_id: str = ""
    storekit_environment: Literal["Production", "Sandbox"] = "Production"
    storekit_app_apple_id: int | None = Field(default=None, gt=0)
    storekit_key_id: str = ""
    storekit_issuer_id: str = ""
    storekit_key_path: str = ""

    @field_validator("app_secret", "app_key", "token_secret", "push_token", "push_aes_key", "auth_identity_secret", "sms_access_key_id", "sms_access_key_secret", mode="before")
    @classmethod
    def empty_secrets_are_disabled(cls, value):
        return None if value == "" else value

    @field_validator("storekit_app_apple_id", mode="before")
    @classmethod
    def empty_app_apple_id(cls, value):
        return None if value == "" else value

    @property
    def login_ready(self) -> bool:
        return bool(self.app_id and self.app_secret and self.token_secret)

    @property
    def sms_ready(self) -> bool:
        return bool(self.sms_login_enabled and self.token_secret and self.auth_identity_secret
                    and self.sms_access_key_id and self.sms_access_key_secret
                    and self.sms_sign_name and self.sms_template_code)

    @property
    def apple_ready(self) -> bool:
        return bool(self.apple_login_enabled and self.token_secret and self.ios_bundle_id)

    @property
    def payment_configured(self) -> bool:
        return bool(self.login_ready and self.offer_id
                    and self.app_key and self.push_token and self.push_aes_key)

    @property
    def payment_ready(self) -> bool:
        return self.payments_enabled and self.payment_configured

    @property
    def storekit_configured(self) -> bool:
        return bool(self.token_secret and self.ios_bundle_id and self.storekit_product_id
                    and self.storekit_key_id and self.storekit_issuer_id and self.storekit_key_path
                    and (self.storekit_environment == "Sandbox" or self.storekit_app_apple_id))

    @model_validator(mode="after")
    def validate_payment_config(self):
        if self.payments_enabled and not self.payment_ready:
            raise ValueError("Rent-room payments require independent login, signing and secure push configuration")
        if self.token_secret and self.app_key and self.token_secret == self.app_key:
            raise ValueError("Rent-room session encryption and payment secrets must be independent")
        if self.sms_login_enabled and not self.sms_ready:
            raise ValueError("SMS login requires provider credentials, approved sign/template and independent identity secret")
        if self.apple_login_enabled and not self.apple_ready:
            raise ValueError("Apple login requires a session secret and the native bundle ID")
        if self.auth_identity_secret and self.auth_identity_secret == self.token_secret:
            raise ValueError("Identity hashing and session secrets must be independent")
        if self.storekit_enabled and not self.storekit_configured:
            raise ValueError("StoreKit requires an independent bundle/product, Apple service credential and environment")
        return self
