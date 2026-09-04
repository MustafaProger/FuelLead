import asyncio
import json

from fastapi.exceptions import RequestValidationError

from app.config import Settings
from app.main import health, safe_validation_error_handler


def test_health_reports_selected_provider_without_exposing_secrets():
    payload = health(
        Settings(
            _env_file=None,
            discovery_provider="api_fns",
            api_fns_key="api-fns-secret",
            checko_api_key="checko-secret",
            okvedo_api_key="okvedo-secret",
            dadata_api_key="dadata-secret",
            dadata_secret_key="dadata-private",
            discovery_limit_per_code=2,
            mail_credentials_encryption_key="encryption-secret",
        )
    )

    assert payload["selected_discovery_provider"] == "combined"
    assert payload["mode"] == "combined"
    assert payload["api_fns_configured"] is True
    assert payload["api_fns_request_budget_per_run"] == {"search": 5, "egr": 5}
    assert payload["checko_configured"] is True
    assert payload["checko_api_key_count"] == 1
    assert payload["checko_state"] == "selected"
    assert payload["okvedo_configured"] is True
    assert payload["dadata_configured"] is True
    assert payload["discovery_provider_order"] == ["checko", "okvedo", "dadata", "api_fns"]
    assert payload["api_fns_fallback_policy"] == "only_after_primary_daily_limits"
    for secret in ("okvedo-secret", "dadata-secret", "dadata-private"):
        assert secret not in str(payload)
    assert payload["target_region_codes"] == ("77", "50")
    assert payload["discovery_limit_per_code"] == 2
    assert "api_fns_key" not in payload
    assert "checko_api_key" not in payload
    assert "api-fns-secret" not in str(payload)
    assert "checko-secret" not in str(payload)
    assert payload["mail_credentials_encryption_configured"] is True
    assert "mail_credentials_encryption_key" not in payload
    assert "encryption-secret" not in str(payload)


def test_validation_errors_never_echo_secret_input():
    response = asyncio.run(
        safe_validation_error_handler(
            None,
            RequestValidationError(
                [
                    {
                        "type": "string_too_long",
                        "loc": ("body", "password"),
                        "msg": "String should have at most 512 characters",
                        "input": "must-never-return-this-secret",
                    }
                ]
            ),
        )
    )
    payload = json.loads(response.body)
    assert "must-never-return-this-secret" not in str(payload)
    assert payload["detail"][0] == {
        "loc": ["body", "password"],
        "msg": "String should have at most 512 characters",
        "type": "string_too_long",
    }
