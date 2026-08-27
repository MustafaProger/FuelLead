import base64
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from app.services.checko import normalize_email


class GmailOAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GmailOAuthConfig:
    sender_email: str
    client_id: str
    client_secret: str
    refresh_token: str
    timeout_seconds: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(
            normalize_email(self.sender_email)
            and self.client_id.strip()
            and self.client_secret.strip()
            and self.refresh_token.strip()
        )


class GmailOAuthSender:
    token_url = "https://oauth2.googleapis.com/token"
    send_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    def __init__(
        self,
        config: GmailOAuthConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        if not config.configured:
            raise ValueError("Gmail OAuth is not configured")
        self.config = config
        self.client = httpx.Client(timeout=config.timeout_seconds, transport=transport)

    def __enter__(self) -> "GmailOAuthSender":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _access_token(self) -> str:
        try:
            response = self.client.post(
                self.token_url,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": self.config.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.RequestError as exc:
            raise GmailOAuthError("Не удалось связаться с Google OAuth") from exc

        if response.is_error:
            raise GmailOAuthError(
                "Google не принял OAuth-доступ. Переподключите Gmail в настройках интеграции."
            )
        token = str(response.json().get("access_token") or "")
        if not token:
            raise GmailOAuthError("Google OAuth не вернул токен доступа")
        return token

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        *,
        html_body: str | None = None,
    ) -> str:
        normalized_recipient = normalize_email(recipient)
        normalized_sender = normalize_email(self.config.sender_email)
        if not normalized_recipient or not normalized_sender:
            raise ValueError("Sender and recipient must be valid email addresses")
        if not subject.strip() or "\n" in subject or "\r" in subject:
            raise ValueError("Subject must be a single non-empty line")
        if not text_body.strip():
            raise ValueError("Email body is required")

        message = EmailMessage()
        message["To"] = normalized_recipient
        message["From"] = normalized_sender
        message["Subject"] = subject.strip()
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            response = self.client.post(
                self.send_url,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json={"raw": raw},
            )
        except httpx.RequestError as exc:
            raise GmailOAuthError("Не удалось отправить письмо через Gmail API") from exc

        if response.is_error:
            raise GmailOAuthError(
                "Gmail API отклонил отправку. Проверьте OAuth-доступ и лимиты аккаунта."
            )
        message_id = str(response.json().get("id") or "")
        if not message_id:
            raise GmailOAuthError("Gmail API не вернул идентификатор письма")
        return message_id
