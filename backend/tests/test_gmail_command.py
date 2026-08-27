from app.commands import send_gmail_test
from app.config import Settings


class FakeSender:
    sent: tuple[str, str, str] | None = None

    def __init__(self, config):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def send(self, recipient: str, subject: str, text_body: str) -> str:
        self.__class__.sent = (recipient, subject, text_body)
        return "test-message-id"


def test_test_email_defaults_to_sender(monkeypatch):
    settings = Settings(
        _env_file=None,
        outreach_sender_email="artel.office8@gmail.com",
        gmail_client_id="client-id",
        gmail_client_secret="client-secret",
        gmail_refresh_token="refresh-token",
    )
    monkeypatch.setattr(send_gmail_test, "GmailOAuthSender", FakeSender)

    recipient, message_id = send_gmail_test.send_test_email(settings)

    assert recipient == "artel.office8@gmail.com"
    assert message_id == "test-message-id"
    assert FakeSender.sent is not None
    assert FakeSender.sent[0] == "artel.office8@gmail.com"
    assert FakeSender.sent[1] == "FuelLead — проверка Gmail API"
    assert "Массовая рассылка не запускалась" in FakeSender.sent[2]


def test_test_email_accepts_explicit_recipient(monkeypatch):
    settings = Settings(
        _env_file=None,
        outreach_sender_email="artel.office8@gmail.com",
        gmail_client_id="client-id",
        gmail_client_secret="client-secret",
        gmail_refresh_token="refresh-token",
    )
    monkeypatch.setattr(send_gmail_test, "GmailOAuthSender", FakeSender)

    recipient, _ = send_gmail_test.send_test_email(settings, "owner@example.com")

    assert recipient == "owner@example.com"
    assert FakeSender.sent is not None
    assert FakeSender.sent[0] == "owner@example.com"
