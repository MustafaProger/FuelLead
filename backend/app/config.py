from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_OKVED_CODES = [
    "42.11",
    "49.41",
    "49.41.1",
    "49.41.2",
    "49.41.3",
    "41.20",
    "01",
    "43.11",
    "43.12",
    "43.12.3",
    "77.32",
    "77.39.1",
    "52.21.2",
]

TARGET_REGION_CODES = ("77", "50")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FuelLead"
    database_url: str = "sqlite:///./fuellead.sqlite3"
    checko_api_key: str = ""
    checko_api_key_fallbacks: str = ""
    checko_base_url: str = "https://api.checko.ru/v2"
    checko_timeout_seconds: float = 30.0
    app_timezone: str = "Europe/Moscow"
    discovery_limit_per_code: int = Field(default=10, ge=1, le=100)
    outreach_sender_email: str = "artel.office8@gmail.com"
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_timeout_seconds: float = 30.0
    fuellead_auth_email: str = ""
    fuellead_auth_password: str = ""
    fuellead_auth_session_secret: str = ""
    fuellead_auth_cookie_days: int = Field(default=3650, ge=1, le=3650)
    fuellead_auth_cookie_secure: bool = False

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def checko_configured(self) -> bool:
        return bool(self.checko_api_keys)

    @property
    def checko_api_keys(self) -> tuple[str, ...]:
        candidates = [self.checko_api_key, *self.checko_api_key_fallbacks.split(",")]
        return tuple(dict.fromkeys(value.strip() for value in candidates if value.strip()))

    @property
    def gmail_oauth_configured(self) -> bool:
        return all(
            value.strip()
            for value in (self.gmail_client_id, self.gmail_client_secret, self.gmail_refresh_token)
        )

    @property
    def auth_configured(self) -> bool:
        return bool(
            self.fuellead_auth_email.strip()
            and self.fuellead_auth_password
            and self.fuellead_auth_session_secret
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
