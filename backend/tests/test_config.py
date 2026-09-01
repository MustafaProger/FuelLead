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


def test_api_fns_can_be_selected_independently_from_checko():
    settings = Settings(
        _env_file=None,
        discovery_provider="api_fns",
        api_fns_key=" api-fns-key ",
        checko_api_key="checko-key",
    )

    assert settings.resolved_discovery_provider == "api_fns"
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
