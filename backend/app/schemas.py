from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.config import DEFAULT_OKVED_CODES
from app.models import MVP_STATUSES


class CompanyFilters(BaseModel):
    status: str | None = None
    has_email: bool | None = None
    category: str | None = None
    discovered_on: date | None = None
    search: str | None = None


class StatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in MVP_STATUSES:
            raise ValueError("MVP status must be one of: new, checked, ready")
        return value


class SearchRunCreate(BaseModel):
    okved_codes: list[str] = Field(default_factory=lambda: DEFAULT_OKVED_CODES.copy(), min_length=1, max_length=30)
    limit_per_code: int = Field(default=10, ge=1, le=100)

    @field_validator("okved_codes")
    @classmethod
    def clean_codes(cls, value: list[str]) -> list[str]:
        codes = list(dict.fromkeys(code.strip() for code in value if code.strip()))
        if not codes:
            raise ValueError("At least one OKVED code is required")
        return codes

