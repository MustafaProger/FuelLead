from app.config import Settings


def test_checko_keys_are_trimmed_and_deduplicated():
    settings = Settings(
        _env_file=None,
        checko_api_key=" primary ",
        checko_api_key_fallbacks="backup, primary, backup-two",
    )

    assert settings.checko_api_keys == ("primary", "backup", "backup-two")
    assert settings.checko_configured is True


def test_provider_auto_mode_preserves_legacy_checko_and_demo_behavior():
    assert Settings(_env_file=None, checko_api_key="key").resolved_discovery_provider == "checko"
    assert Settings(_env_file=None, checko_api_key="").resolved_discovery_provider == "demo"


def test_api_fns_cannot_bypass_configured_checko():
    settings = Settings(
        _env_file=None,
        discovery_provider="api_fns",
        api_fns_key=" api-fns-key ",
        checko_api_key="checko-key",
    )

    assert settings.resolved_discovery_provider == "combined"
    assert settings.api_fns_configured is True
    assert settings.checko_configured is True


def test_combined_provider_requires_no_extra_key_setting():
    settings = Settings(
        _env_file=None,
        discovery_provider="combined",
        api_fns_key="api-fns-key",
        checko_api_key="checko-key",
    )

    assert settings.resolved_discovery_provider == "combined"


def test_outreach_defaults_keep_automatic_send_disabled_and_use_safe_timers():
    settings = Settings(_env_file=None)

    assert settings.outreach_automatic_send_enabled is False
    assert settings.outreach_campaign_size == 500
    assert settings.outreach_daily_limit == 500
    assert settings.outreach_hourly_limit == 0
    assert settings.outreach_min_interval_seconds == 10
    assert settings.outreach_max_per_domain_per_day == 0
    assert settings.outreach_message_interval_min_seconds == 60
    assert settings.outreach_message_interval_max_seconds == 85
    assert settings.outreach_round_rest_min_minutes == 77
    assert settings.outreach_round_rest_max_minutes == 93
    assert settings.outreach_snapshot_ttl_seconds == 600


def test_auto_uses_new_providers_and_fns_last():
    settings = Settings(_env_file=None, okvedo_api_key="ok", dadata_api_key="da", api_fns_key="fns")
    assert settings.primary_discovery_providers == ("okvedo", "dadata")
    assert settings.resolved_discovery_provider == "combined"
    assert Settings(_env_file=None, okvedo_api_key="ok").resolved_discovery_provider == "okvedo"
    assert Settings(_env_file=None, dadata_api_key="da").resolved_discovery_provider == "dadata"
    assert Settings(_env_file=None, api_fns_key="fns").resolved_discovery_provider == "api_fns"
