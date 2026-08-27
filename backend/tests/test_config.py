from app.config import Settings


def test_checko_keys_are_trimmed_and_deduplicated():
    settings = Settings(
        _env_file=None,
        checko_api_key=" primary ",
        checko_api_key_fallbacks="backup, primary, backup-two",
    )

    assert settings.checko_api_keys == ("primary", "backup", "backup-two")
    assert settings.checko_configured is True
