import base64
import json

import httpx

from app.services.gmail import GmailOAuthConfig, GmailOAuthSender


def test_gmail_sender_uses_oauth_and_sender_address():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-token"})
        assert request.headers["Authorization"] == "Bearer access-token"
        raw = json.loads(request.content)["raw"]
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        assert "From: artel.office8@gmail.com" in decoded
        assert "To: lead@example.ru" in decoded
        assert "Subject: FuelLead" in decoded
        return httpx.Response(200, json={"id": "gmail-message-id"})

    config = GmailOAuthConfig(
        sender_email="artel.office8@gmail.com",
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
    )
    with GmailOAuthSender(config, transport=httpx.MockTransport(handler)) as sender:
        message_id = sender.send("lead@example.ru", "FuelLead", "Тестовое письмо")

    assert message_id == "gmail-message-id"
    assert [request.url.host for request in requests] == [
        "oauth2.googleapis.com",
        "gmail.googleapis.com",
    ]
