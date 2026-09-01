from functools import lru_cache
from typing import Literal
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
    discovery_provider: Literal["auto", "demo", "checko", "api_fns", "combined"] = "auto"
    checko_api_key: str = ""
    checko_api_key_fallbacks: str = ""
    checko_base_url: str = "https://api.checko.ru/v2"
    checko_timeout_seconds: float = 30.0
    api_fns_key: str = ""
    api_fns_base_url: str = "https://api-fns.ru/api"
    api_fns_timeout_seconds: float = 30.0
    api_fns_require_phone: bool = False
    api_fns_require_email: bool = False
    api_fns_max_search_requests_per_run: int = Field(default=5, ge=1, le=100)
    api_fns_max_egr_requests_per_run: int = Field(default=5, ge=1, le=100)
    app_timezone: str = "Europe/Moscow"
    discovery_limit_per_code: int = Field(default=10, ge=1, le=100)
    outreach_sender_email: str = "artel.office8@gmail.com"
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_timeout_seconds: float = 30.0
    outreach_campaign_size: int = Field(default=20, ge=1, le=100)
    outreach_daily_limit: int = Field(default=20, ge=1, le=100)
    outreach_hourly_limit: int = Field(default=5, ge=1, le=20)
    outreach_min_interval_seconds: int = Field(default=300, ge=60, le=86_400)
    outreach_max_per_domain_per_day: int = Field(default=2, ge=1, le=20)
    outreach_worker_poll_seconds: int = Field(default=30, ge=5, le=300)
    outreach_opt_out_text: str = (
        "Если предложение неактуально, ответьте «Не писать», "
        "и мы исключим адрес из дальнейших обращений."
    )
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
    def api_fns_configured(self) -> bool:
        return bool(self.api_fns_key.strip())

    @property
    def resolved_discovery_provider(self) -> Literal["demo", "checko", "api_fns", "combined"]:
        if self.discovery_provider == "auto":
            return "checko" if self.checko_configured else "demo"
        return self.discovery_provider

    @property
    def gmail_oauth_configured(self) -> bool:
        return all(
            value.strip()
            for value in (self.gmail_client_id, self.gmail_client_secret, self.gmail_refresh_token)
        )

    @property
    def outreach_batch_limit(self) -> int:
        return min(self.outreach_campaign_size, self.outreach_daily_limit)

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
