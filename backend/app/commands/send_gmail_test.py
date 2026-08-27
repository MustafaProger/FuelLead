import argparse
from datetime import datetime

from app.config import Settings, get_settings
from app.services.gmail import GmailOAuthConfig, GmailOAuthError, GmailOAuthSender


def send_test_email(settings: Settings, recipient: str | None = None) -> tuple[str, str]:
    target = recipient or settings.outreach_sender_email
    checked_at = datetime.now(settings.timezone).strftime("%d.%m.%Y %H:%M:%S %Z")
    config = GmailOAuthConfig(
        sender_email=settings.outreach_sender_email,
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        refresh_token=settings.gmail_refresh_token,
        timeout_seconds=settings.gmail_timeout_seconds,
    )
    body = (
        "Тестовое письмо FuelLead.\n\n"
        "Gmail API и OAuth 2.0 работают.\n"
        f"Время проверки: {checked_at}.\n\n"
        "Массовая рассылка не запускалась."
    )
    with GmailOAuthSender(config) as sender:
        message_id = sender.send(
            target,
            "FuelLead — проверка Gmail API",
            body,
        )
    return target, message_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отправить одно тестовое письмо через настроенный Gmail OAuth 2.0.",
    )
    parser.add_argument(
        "--recipient",
        help="Адрес получателя. По умолчанию письмо отправляется самому отправителю.",
    )
    args = parser.parse_args()

    try:
        recipient, message_id = send_test_email(get_settings(), args.recipient)
    except (GmailOAuthError, ValueError) as exc:
        parser.exit(1, f"Ошибка Gmail: {exc}\n")

    print(f"Письмо отправлено: recipient={recipient}, message_id={message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
