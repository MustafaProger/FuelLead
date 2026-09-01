from app.config import Settings
from app.main import health


def test_health_reports_selected_provider_without_exposing_secrets():
    payload = health(
        Settings(
            _env_file=None,
            discovery_provider="api_fns",
            api_fns_key="api-fns-secret",
            checko_api_key="checko-secret",
            discovery_limit_per_code=2,
        )
    )

    assert payload["selected_discovery_provider"] == "api_fns"
    assert payload["mode"] == "api_fns"
    assert payload["api_fns_configured"] is True
    assert payload["api_fns_request_budget_per_run"] == {"search": 5, "egr": 5}
    assert payload["checko_configured"] is True
    assert payload["checko_api_key_count"] == 1
    assert payload["checko_state"] == "standby"
    assert payload["target_region_codes"] == ("77", "50")
    assert payload["discovery_limit_per_code"] == 2
    assert "api_fns_key" not in payload
    assert "checko_api_key" not in payload
    assert "api-fns-secret" not in str(payload)
    assert "checko-secret" not in str(payload)
