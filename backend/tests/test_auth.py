from fastapi.testclient import TestClient

from app.auth import AUTH_COOKIE_NAME, create_session_token, credentials_match, session_email
from app.config import Settings, get_settings
from app.main import app


def auth_settings() -> Settings:
    return Settings(
        _env_file=None,
        fuellead_auth_email="operator@example.com",
        fuellead_auth_password="correct-password",
        fuellead_auth_session_secret="test-session-secret-that-is-not-used-in-production",
        fuellead_auth_cookie_days=3650,
    )


def test_credentials_match_is_case_insensitive_only_for_email():
    settings = auth_settings()

    assert credentials_match(" Operator@Example.com ", "correct-password", settings)
    assert not credentials_match("operator@example.com", "Correct-Password", settings)
    assert not credentials_match("other@example.com", "correct-password", settings)


def test_signed_session_token_survives_without_server_state_and_rejects_tampering():
    settings = auth_settings()
    token = create_session_token(settings.fuellead_auth_email, settings)

    assert session_email(token, settings) == settings.fuellead_auth_email
    assert session_email(f"{token}tampered", settings) is None
    assert session_email(token, settings.model_copy(update={"fuellead_auth_session_secret": "rotated"})) is None
    assert session_email(token, settings.model_copy(update={"fuellead_auth_password": "rotated"})) is None


def test_protected_api_login_session_and_logout_flow(monkeypatch):
    settings = auth_settings()
    client = TestClient(app)

    try:
        app.dependency_overrides[get_settings] = lambda: settings
        monkeypatch.setattr("app.main.get_settings", lambda: settings)
        unauthorized = client.get("/api/health")
        assert unauthorized.status_code == 401

        invalid = client.post(
            "/api/auth/login",
            json={"email": settings.fuellead_auth_email, "password": "wrong-password"},
        )
        assert invalid.status_code == 401

        login = client.post(
            "/api/auth/login",
            json={
                "email": settings.fuellead_auth_email,
                "password": settings.fuellead_auth_password,
            },
        )
        assert login.status_code == 200
        assert login.json() == {"authenticated": True, "email": settings.fuellead_auth_email}
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert "Max-Age=315360000" in login.headers["set-cookie"]

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["email"] == settings.fuellead_auth_email
        assert client.get("/api/health").status_code == 200

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert client.get("/api/health").status_code == 401
    finally:
        app.dependency_overrides.clear()
